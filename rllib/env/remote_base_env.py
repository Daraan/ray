import gymnasium as gym
import logging
from typing import Callable, Dict, List, Optional, Set, Tuple, TYPE_CHECKING

import ray
from ray.util import log_once
from ray.rllib.env.base_env import BaseEnv, _DUMMY_AGENT_ID, ASYNC_RESET_RETURN
from ray.rllib.utils.annotations import override, OldAPIStack
from ray.rllib.utils.typing import AgentID, EnvID, EnvType, MultiEnvDict

if TYPE_CHECKING:
    from ray.rllib.evaluation.rollout_worker import RolloutWorker

logger = logging.getLogger(__name__)


@OldAPIStack
class RemoteBaseEnv(BaseEnv):
    """BaseEnv that executes its sub environments as @ray.remote actors.

    This provides dynamic batching of inference as observations are returned
    from the remote simulator actors. Both single and multi-agent child envs
    are supported, and envs can be stepped synchronously or asynchronously.

    NOTE: This class implicitly assumes that the remote envs are gym.Env's

    You shouldn't need to instantiate this class directly. It's automatically
    inserted when you use the `remote_worker_envs=True` option in your
    Algorithm's config.
    """

    def __init__(
        self,
        make_env: Callable[[int], EnvType],
        num_envs: int,
        multiagent: bool,
        remote_env_batch_wait_ms: int,
        existing_envs: Optional[List[ray.actor.ActorHandle]] = None,
        worker: Optional["RolloutWorker"] = None,
        restart_failed_sub_environments: bool = False,
    ):
        """Initializes a RemoteVectorEnv instance.

        Args:
            make_env: Callable that produces a single (non-vectorized) env,
                given the vector env index as only arg.
            num_envs: The number of sub-environments to create for the
                vectorization.
            multiagent: Whether this is a multiagent env or not.
            remote_env_batch_wait_ms: Time to wait for (ray.remote)
                sub-environments to have new observations available when
                polled. Only when none of the sub-environments is ready,
                repeat the `ray.wait()` call until at least one sub-env
                is ready. Then return only the observations of the ready
                sub-environment(s).
            existing_envs: Optional list of already created sub-environments.
                These will be used as-is and only as many new sub-envs as
                necessary (`num_envs - len(existing_envs)`) will be created.
            worker: An optional RolloutWorker that owns the env. This is only
                used if `remote_worker_envs` is True in your config and the
                `on_sub_environment_created` custom callback needs to be
                called on each created actor.
            restart_failed_sub_environments: If True and any sub-environment (within
                a vectorized env) throws any error during env stepping, the
                Sampler will try to restart the faulty sub-environment. This is done
                without disturbing the other (still intact) sub-environment and without
                the RolloutWorker crashing.
        """

        # Could be creating local or remote envs.
        self.make_env = make_env
        self.num_envs = num_envs
        self.multiagent = multiagent
        self.poll_timeout = remote_env_batch_wait_ms / 1000
        self.worker = worker
        self.restart_failed_sub_environments = restart_failed_sub_environments

        # Already existing env objects (generated by the RolloutWorker).
        existing_envs = existing_envs or []

        # Whether the given `make_env` callable already returns ActorHandles
        # (@ray.remote class instances) or not.
        self.make_env_creates_actors = False

        self._observation_space = None
        self._action_space = None

        # List of ray actor handles (each handle points to one @ray.remote
        # sub-environment).
        self.actors: Optional[List[ray.actor.ActorHandle]] = None

        # `self.make_env` already produces Actors: Use it directly.
        if len(existing_envs) > 0 and isinstance(
            existing_envs[0], ray.actor.ActorHandle
        ):
            self.make_env_creates_actors = True
            self.actors = existing_envs
            while len(self.actors) < self.num_envs:
                self.actors.append(self._make_sub_env(len(self.actors)))

        # `self.make_env` produces gym.Envs (or children thereof, such
        # as MultiAgentEnv): Need to auto-wrap it here. The problem with
        # this is that custom methods wil get lost. If you would like to
        # keep your custom methods in your envs, you should provide the
        # env class directly in your config (w/o tune.register_env()),
        # such that your class can directly be made a @ray.remote
        # (w/o the wrapping via `_Remote[Multi|Single]AgentEnv`).
        # Also, if `len(existing_envs) > 0`, we have to throw those away
        # as we need to create ray actors here.
        else:
            self.actors = [self._make_sub_env(i) for i in range(self.num_envs)]
            # Utilize existing envs for inferring observation/action spaces.
            if len(existing_envs) > 0:
                self._observation_space = existing_envs[0].observation_space
                self._action_space = existing_envs[0].action_space
            # Have to call actors' remote methods to get observation/action spaces.
            else:
                self._observation_space, self._action_space = ray.get(
                    [
                        self.actors[0].observation_space.remote(),
                        self.actors[0].action_space.remote(),
                    ]
                )

        # Dict mapping object refs (return values of @ray.remote calls),
        # whose actual values we are waiting for (via ray.wait in
        # `self.poll()`) to their corresponding actor handles (the actors
        # that created these return values).
        # Call `reset()` on all @ray.remote sub-environment actors.
        self.pending: Dict[ray.actor.ActorHandle] = {
            a.reset.remote(): a for a in self.actors
        }

    @override(BaseEnv)
    def poll(
        self,
    ) -> Tuple[
        MultiEnvDict,
        MultiEnvDict,
        MultiEnvDict,
        MultiEnvDict,
        MultiEnvDict,
        MultiEnvDict,
    ]:

        # each keyed by env_id in [0, num_remote_envs)
        obs, rewards, terminateds, truncateds, infos = {}, {}, {}, {}, {}
        ready = []

        # Wait for at least 1 env to be ready here.
        while not ready:
            ready, _ = ray.wait(
                list(self.pending),
                num_returns=len(self.pending),
                timeout=self.poll_timeout,
            )

        # Get and return observations for each of the ready envs
        env_ids = set()
        for obj_ref in ready:
            # Get the corresponding actor handle from our dict and remove the
            # object ref (we will call `ray.get()` on it and it will no longer
            # be "pending").
            actor = self.pending.pop(obj_ref)
            env_id = self.actors.index(actor)
            env_ids.add(env_id)
            # Get the ready object ref (this may be return value(s) of
            # `reset()` or `step()`).
            try:
                ret = ray.get(obj_ref)
            except Exception as e:
                # Something happened on the actor during stepping/resetting.
                # Restart sub-environment (create new actor; close old one).
                if self.restart_failed_sub_environments:
                    logger.exception(e.args[0])
                    self.try_restart(env_id)
                    # Always return multi-agent data.
                    # Set the observation to the exception, no rewards,
                    # terminated[__all__]=True (episode will be discarded anyways),
                    # no infos.
                    ret = (
                        e,
                        {},
                        {"__all__": True},
                        {"__all__": False},
                        {},
                    )
                # Do not try to restart. Just raise the error.
                else:
                    raise e

            # Our sub-envs are simple Actor-turned gym.Envs or MultiAgentEnvs.
            if self.make_env_creates_actors:
                rew, terminated, truncated, info = None, None, None, None
                if self.multiagent:
                    if isinstance(ret, tuple):
                        # Gym >= 0.26: `step()` result: Obs, reward, terminated,
                        # truncated, info.
                        if len(ret) == 5:
                            ob, rew, terminated, truncated, info = ret
                        # Gym >= 0.26: `reset()` result: Obs and infos.
                        elif len(ret) == 2:
                            ob = ret[0]
                            info = ret[1]
                        # Gym < 0.26? Something went wrong.
                        else:
                            raise AssertionError(
                                "Your gymnasium.Env seems to NOT return the correct "
                                "number of return values for `step()` (needs to return"
                                " 5 values: obs, reward, terminated, truncated and "
                                "info) or `reset()` (needs to return 2 values: obs and "
                                "info)!"
                            )
                    # Gym < 0.26: `reset()` result: Only obs.
                    else:
                        raise AssertionError(
                            "Your gymnasium.Env seems to only return a single value "
                            "upon `reset()`! Must return 2 (obs AND infos)."
                        )
                else:
                    if isinstance(ret, tuple):
                        # `step()` result: Obs, reward, terminated, truncated, info.
                        if len(ret) == 5:
                            ob = {_DUMMY_AGENT_ID: ret[0]}
                            rew = {_DUMMY_AGENT_ID: ret[1]}
                            terminated = {_DUMMY_AGENT_ID: ret[2], "__all__": ret[2]}
                            truncated = {_DUMMY_AGENT_ID: ret[3], "__all__": ret[3]}
                            info = {_DUMMY_AGENT_ID: ret[4]}
                        # `reset()` result: Obs and infos.
                        elif len(ret) == 2:
                            ob = {_DUMMY_AGENT_ID: ret[0]}
                            info = {_DUMMY_AGENT_ID: ret[1]}
                        # Gym < 0.26? Something went wrong.
                        else:
                            raise AssertionError(
                                "Your gymnasium.Env seems to NOT return the correct "
                                "number of return values for `step()` (needs to return"
                                " 5 values: obs, reward, terminated, truncated and "
                                "info) or `reset()` (needs to return 2 values: obs and "
                                "info)!"
                            )
                    # Gym < 0.26?
                    else:
                        raise AssertionError(
                            "Your gymnasium.Env seems to only return a single value "
                            "upon `reset()`! Must return 2 (obs and infos)."
                        )

                # If this is a `reset()` return value, we only have the initial
                # observations and infos: Set rewards, terminateds, and truncateds to
                # dummy values.
                if rew is None:
                    rew = {agent_id: 0 for agent_id in ob.keys()}
                    terminated = {"__all__": False}
                    truncated = {"__all__": False}

            # Our sub-envs are auto-wrapped (by `_RemoteSingleAgentEnv` or
            # `_RemoteMultiAgentEnv`) and already behave like multi-agent
            # envs.
            else:
                ob, rew, terminated, truncated, info = ret
            obs[env_id] = ob
            rewards[env_id] = rew
            terminateds[env_id] = terminated
            truncateds[env_id] = truncated
            infos[env_id] = info

        logger.debug(f"Got obs batch for actors {env_ids}")
        return obs, rewards, terminateds, truncateds, infos, {}

    @override(BaseEnv)
    def send_actions(self, action_dict: MultiEnvDict) -> None:
        for env_id, actions in action_dict.items():
            actor = self.actors[env_id]
            # `actor` is a simple single-agent (remote) env, e.g. a gym.Env
            # that was made a @ray.remote.
            if not self.multiagent and self.make_env_creates_actors:
                obj_ref = actor.step.remote(actions[_DUMMY_AGENT_ID])
            # `actor` is already a _RemoteSingleAgentEnv or
            # _RemoteMultiAgentEnv wrapper
            # (handles the multi-agent action_dict automatically).
            else:
                obj_ref = actor.step.remote(actions)
            self.pending[obj_ref] = actor

    @override(BaseEnv)
    def try_reset(
        self,
        env_id: Optional[EnvID] = None,
        *,
        seed: Optional[int] = None,
        options: Optional[dict] = None,
    ) -> Tuple[MultiEnvDict, MultiEnvDict]:
        actor = self.actors[env_id]
        obj_ref = actor.reset.remote(seed=seed, options=options)

        self.pending[obj_ref] = actor
        # Because this env type does not support synchronous reset requests (with
        # immediate return value), we return ASYNC_RESET_RETURN here to indicate
        # that the reset results will be available via the next `poll()` call.
        return ASYNC_RESET_RETURN, ASYNC_RESET_RETURN

    @override(BaseEnv)
    def try_restart(self, env_id: Optional[EnvID] = None) -> None:
        # Try closing down the old (possibly faulty) sub-env, but ignore errors.
        try:
            # Close the env on the remote side.
            self.actors[env_id].close.remote()
        except Exception as e:
            if log_once("close_sub_env"):
                logger.warning(
                    "Trying to close old and replaced sub-environment (at vector "
                    f"index={env_id}), but closing resulted in error:\n{e}"
                )

        # Terminate the actor itself to free up its resources.
        self.actors[env_id].__ray_terminate__.remote()

        # Re-create a new sub-environment.
        self.actors[env_id] = self._make_sub_env(env_id)

    @override(BaseEnv)
    def stop(self) -> None:
        if self.actors is not None:
            for actor in self.actors:
                actor.__ray_terminate__.remote()

    @override(BaseEnv)
    def get_sub_environments(self, as_dict: bool = False) -> List[EnvType]:
        if as_dict:
            return dict(enumerate(self.actors))
        return self.actors

    @property
    @override(BaseEnv)
    def observation_space(self) -> gym.spaces.Dict:
        return self._observation_space

    @property
    @override(BaseEnv)
    def action_space(self) -> gym.Space:
        return self._action_space

    def _make_sub_env(self, idx: Optional[int] = None):
        """Re-creates a sub-environment at the new index."""

        # Our `make_env` creates ray actors directly.
        if self.make_env_creates_actors:
            sub_env = self.make_env(idx)
            if self.worker is not None:
                self.worker.callbacks.on_sub_environment_created(
                    worker=self.worker,
                    sub_environment=self.actors[idx],
                    env_context=self.worker.env_context.copy_with_overrides(
                        vector_index=idx
                    ),
                )

        # Our `make_env` returns actual envs -> Have to convert them into actors
        # using our utility wrapper classes.
        else:

            def make_remote_env(i):
                logger.info("Launching env {} in remote actor".format(i))
                if self.multiagent:
                    sub_env = _RemoteMultiAgentEnv.remote(self.make_env, i)
                else:
                    sub_env = _RemoteSingleAgentEnv.remote(self.make_env, i)

                if self.worker is not None:
                    self.worker.callbacks.on_sub_environment_created(
                        worker=self.worker,
                        sub_environment=sub_env,
                        env_context=self.worker.env_context.copy_with_overrides(
                            vector_index=i
                        ),
                    )

                return sub_env

            sub_env = make_remote_env(idx)

        return sub_env

    @override(BaseEnv)
    def get_agent_ids(self) -> Set[AgentID]:
        if self.multiagent:
            return ray.get(self.actors[0].get_agent_ids.remote())
        else:
            return {_DUMMY_AGENT_ID}


@ray.remote(num_cpus=0)
class _RemoteMultiAgentEnv:
    """Wrapper class for making a multi-agent env a remote actor."""

    def __init__(self, make_env, i):
        self.env = make_env(i)
        self.agent_ids = set()

    def reset(self, *, seed: Optional[int] = None, options: Optional[dict] = None):
        obs, info = self.env.reset(seed=seed, options=options)

        # each keyed by agent_id in the env
        rew = {}
        for agent_id in obs.keys():
            self.agent_ids.add(agent_id)
            rew[agent_id] = 0.0
        terminated = {"__all__": False}
        truncated = {"__all__": False}
        return obs, rew, terminated, truncated, info

    def step(self, action_dict):
        return self.env.step(action_dict)

    # Defining these 2 functions that way this information can be queried
    # with a call to ray.get().
    def observation_space(self):
        return self.env.observation_space

    def action_space(self):
        return self.env.action_space

    def get_agent_ids(self) -> Set[AgentID]:
        return self.agent_ids


@ray.remote(num_cpus=0)
class _RemoteSingleAgentEnv:
    """Wrapper class for making a gym env a remote actor."""

    def __init__(self, make_env, i):
        self.env = make_env(i)

    def reset(self, *, seed: Optional[int] = None, options: Optional[dict] = None):
        obs_and_info = self.env.reset(seed=seed, options=options)

        obs = {_DUMMY_AGENT_ID: obs_and_info[0]}
        info = {_DUMMY_AGENT_ID: obs_and_info[1]}

        rew = {_DUMMY_AGENT_ID: 0.0}
        terminated = {"__all__": False}
        truncated = {"__all__": False}
        return obs, rew, terminated, truncated, info

    def step(self, action):
        results = self.env.step(action[_DUMMY_AGENT_ID])

        obs, rew, terminated, truncated, info = [{_DUMMY_AGENT_ID: x} for x in results]

        terminated["__all__"] = terminated[_DUMMY_AGENT_ID]
        truncated["__all__"] = truncated[_DUMMY_AGENT_ID]

        return obs, rew, terminated, truncated, info

    # Defining these 2 functions that way this information can be queried
    # with a call to ray.get().
    def observation_space(self):
        return self.env.observation_space

    def action_space(self):
        return self.env.action_space
