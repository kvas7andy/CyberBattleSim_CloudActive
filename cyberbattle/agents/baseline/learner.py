# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

"""Learner helpers and epsilon greedy search"""
import math
import sys
import os
import re

from .plotting import PlotTraining, plot_averaged_cummulative_rewards
from .agent_wrapper import AgentWrapper, EnvironmentBounds, Verbosity, ActionTrackingStateAugmentation
from cyberbattle.simulation.config import logger, configuration
import numpy as np
import torch
import random
from cyberbattle._env import cyberbattle_env
from typing import Tuple, Optional, TypedDict, List
import progressbar
import abc
from torch.utils.tensorboard.summary import hparams


class Learner(abc.ABC):
    """Interface to be implemented by an epsilon-greedy learner"""

    def new_episode(self) -> None:
        return None

    def end_of_episode(self, i_episode, t) -> None:
        return None

    def end_of_iteration(self, t, done) -> None:
        return None

    @abc.abstractmethod
    def explore(self, wrapped_env: AgentWrapper) -> Tuple[str, cyberbattle_env.Action, object]:
        """Exploration function.
        Returns (action_type, gym_action, action_metadata) where
        action_metadata is a custom object that gets passed to the on_step callback function"""
        raise NotImplementedError

    @abc.abstractmethod
    def exploit(self, wrapped_env: AgentWrapper, observation) -> Tuple[str, Optional[cyberbattle_env.Action], object]:
        """Exploit function.
        Returns (action_type, gym_action, action_metadata) where
        action_metadata is a custom object that gets passed to the on_step callback function"""
        raise NotImplementedError

    @abc.abstractmethod
    def on_step(self, wrapped_env: AgentWrapper, observation, reward, done, info, action_metadata) -> None:
        raise NotImplementedError

    def train(self) -> None:
        return

    def eval(self) -> None:
        return

    def save(self, filename) -> None:
        return

    def load_best(self, filename) -> None:
        return

    def parameters_as_string(self) -> str:
        return ''

    def all_parameters_as_string(self) -> str:
        return ''

    def loss_as_string(self) -> str:
        return ''

    def stateaction_as_string(self, action_metadata) -> str:
        return ''

    def name(self) -> str:
        """Return the name of the agent"""
        # p = len(type(Learner(self.env_properties, [])).__name__) + 1
        return type(self).__name__


class RandomPolicy(Learner):
    """A policy that does not learn and only explore"""

    def explore(self, wrapped_env: AgentWrapper) -> Tuple[str, cyberbattle_env.Action, object]:
        gym_action = wrapped_env.env.sample_valid_action()
        return "explore", gym_action, None

    def exploit(self, wrapped_env: AgentWrapper, observation) -> Tuple[str, Optional[cyberbattle_env.Action], object]:
        gym_action = wrapped_env.env.sample_valid_action()
        return "explore", gym_action, None

    def on_step(self, wrapped_env: AgentWrapper, observation, reward, done, info, action_metadata):
        return None


Breakdown = TypedDict('Breakdown', {
    'local': int,
    'remote': int,
    'connect': int
})

Outcomes = TypedDict('Outcomes', {
    'reward': Breakdown,
    'noreward': Breakdown
})

Stats = TypedDict('Stats', {
    'exploit': Outcomes,
    'explore': Outcomes,
    'exploit_deflected_to_explore': int
})

TrainedLearner = TypedDict('TrainedLearner', {
    'all_episodes_rewards': List[List[float]],
    'all_episodes_availability': List[List[float]],
    'learner': Learner,
    'trained_on': str,
    'title': str,
    'best_running_mean': float
})


def write_to_summary(writer, all_rewards, epsilon, loss_string, observation, iteration_count, run_mean, steps_done, writer_tag="training"):
    """
    all_rewards: - (training case) list of rewards per episode; (evaluation case) list of sum of rewards during episode
    """

    is_training = writer_tag == "training"

    total_reward = sum(all_rewards)
    # TODO: make higher verbosity level
    # writer.add_histogram(writer_tag + "/rewards", all_rewards, steps_done)
    writer.add_scalar(writer_tag + "/epsilon", epsilon, steps_done) if is_training else ''
    writer.add_scalar("loss", float(re.sub(r'[^a-zA-Z0-9]', '', loss_string.split("=")[-1])), steps_done) if is_training and loss_string else ''

    n_positive_actions = np.sum(np.array(all_rewards) > 0)
    writer.add_scalar(writer_tag + "/n_positive_actions", n_positive_actions, steps_done)
    writer.add_scalar("total_reward", total_reward, steps_done) if is_training else ''
    writer.add_scalar(writer_tag + "/total_reward", total_reward, steps_done) if not is_training else ''
    # writer.add_text(writer_tag + "/deception_tracker", str({k: v.trigger_times for k, v in observation['_deception_tracker'].items()}), steps_done)
    for k, v in observation['_deception_tracker'].items():
        writer.add_scalar(writer_tag + "/detection_points_trigger_counter/" + k, len(v.trigger_times), steps_done)
        # writer.add_histogram(writer_tag + "/detection_points_trigger_steps/" + k,
        #                      np.array(v.trigger_times), steps_done, bins=iteration_count) if len(v.trigger_times) else ''
    # triggers = [v.trigger_times for _, v in observation['_deception_tracker'].items()]
    # writer.add_histogram(writer_tag + "/detection_points_trigger_steps",
    #                      np.concatenate(triggers), steps_done, bins=iteration_count) if len(triggers) and len(np.concatenate(triggers)) else ''
    if is_training:
        writer.add_scalar("run_mean", run_mean, steps_done)
    else:
        writer.add_scalar("eval_run_mean", run_mean, steps_done)

    writer.flush()


def print_stats(stats):
    """Print learning statistics"""
    def print_breakdown(stats, actiontype: str):
        def ratio(kind: str) -> str:
            x, y = stats[actiontype]['reward'][kind], stats[actiontype]['noreward'][kind]
            sum = x + y
            if sum == 0:
                return 'NaN'
            else:
                return f"{(x / sum):.2f}"

        def print_kind(kind: str):
            print(
                f"    {actiontype}-{kind}: {stats[actiontype]['reward'][kind]}/{stats[actiontype]['noreward'][kind]} "
                f"({ratio(kind)})")
        print_kind('local')
        print_kind('remote')
        print_kind('connect')

    print("  Breakdown [Reward/NoReward (Success rate)]")
    print_breakdown(stats, 'explore')
    print_breakdown(stats, 'exploit')
    print(f"  exploit deflected to exploration: {stats['exploit_deflected_to_explore']}")


def evaluate_model(
    cyberbattle_gym_env: cyberbattle_env.CyberBattleEnv,
    environment_properties: EnvironmentBounds,
    learner: Learner,
    title: str,
    iteration_count: int,
    epsilon: float,
    eval_episode_count: int,
    best_eval_running_mean: float,
    eval_freq: Optional[int] = 5,
    training_steps_done: int = 0,
    training_episode_done: int = 0,
    mean_reward_window: int = 10,
    render=True,
    render_last_episode_rewards_to: Optional[str] = None,
    verbosity: Verbosity = Verbosity.Normal,
    save_model_filename=""
) -> TrainedLearner:
    writer = configuration.writer

    print(f"###### {title}\n"
          f"Evaluating with: eval_episode_count={eval_episode_count},"
          f"training_episode_done={training_episode_done}",
          f"iteration_count={iteration_count},"
          f"ϵ={epsilon},"
          f"{learner.parameters_as_string()}")

    initial_epsilon = epsilon

    all_episodes_rewards = []
    all_episodes_sum_rewards = []
    all_episodes_availability = []

    wrapped_env = AgentWrapper(cyberbattle_gym_env,
                               ActionTrackingStateAugmentation(environment_properties, cyberbattle_gym_env.reset()))
    steps_done = 0

    plot_title = f"{title} (epochs={eval_episode_count}, ϵ={initial_epsilon}" + learner.parameters_as_string()

    render_file_index = 1
    learner.eval()

    if configuration.log_results:
        detection_points_results = {}

    for i_episode in range(1, eval_episode_count + 1):

        print(f"  ## Episode: {i_episode}/{eval_episode_count} '{title}' "
              f"ϵ={epsilon:.4f}, "
              f"{learner.parameters_as_string()}")

        observation = wrapped_env.reset()
        total_reward = 0.0
        all_rewards = []
        all_availability = []
        learner.new_episode()

        stats = Stats(exploit=Outcomes(reward=Breakdown(local=0, remote=0, connect=0),
                                       noreward=Breakdown(local=0, remote=0, connect=0)),
                      explore=Outcomes(reward=Breakdown(local=0, remote=0, connect=0),
                                       noreward=Breakdown(local=0, remote=0, connect=0)),
                      exploit_deflected_to_explore=0
                      )

        episode_ended_at = None
        sys.stdout.flush()

        for t in range(1, 1 + iteration_count):

            steps_done += 1

            # x = np.random.rand()
            # if x <= epsilon:
            #     action_style, gym_action, action_metadata = learner.explore(wrapped_env)
            # else:
            action_style, gym_action, action_metadata = learner.exploit(wrapped_env, observation)
            if not gym_action:
                stats['exploit_deflected_to_explore'] += 1
                _, gym_action, action_metadata = learner.explore(wrapped_env)  # TODO: evaluation - exclude gym_aciton is None due to 1) NN no candidates 2)  > n_discovered_nodes, > n_credential_cache

            # Take the step
            # logger.debug(f"gym_action={gym_action}, action_metadata={action_metadata}") if configuration.log_results else None
            observation, reward, done, info = wrapped_env.step(gym_action)

            action_type = 'exploit' if action_style == 'exploit' else 'explore'
            outcome = 'reward' if reward > 0 else 'noreward'
            if 'local_vulnerability' in gym_action:
                stats[action_type][outcome]['local'] += 1
            elif 'remote_vulnerability' in gym_action:
                stats[action_type][outcome]['remote'] += 1
            else:
                stats[action_type][outcome]['connect'] += 1

            # learner.on_step(wrapped_env, observation, reward, done, info, action_metadata)
            assert np.shape(reward) == ()

            all_rewards.append(reward)
            all_availability.append(info['network_availability'])
            total_reward += reward
            # bar.update(t, reward=total_reward)
            # if reward > 0:
            # bar.update(t, last_reward_at=t)

            if verbosity == Verbosity.Verbose or (verbosity == Verbosity.Normal and reward > 0):
                sign = ['-', '+'][reward > 0]

                print(f"    {sign} t={t} {action_style} r={reward} total_reward:{total_reward} "
                      f"a={action_metadata}-{gym_action} "
                      f"creds={len(observation['credential_cache_matrix'])} "
                      f" {learner.stateaction_as_string(action_metadata)}")

            if i_episode == eval_episode_count \
                    and render_last_episode_rewards_to is not None \
                    and reward > 0:
                fig = cyberbattle_gym_env.render_as_fig()
                fig.write_image(f"{render_last_episode_rewards_to}-e{i_episode}-{render_file_index}.png")
                render_file_index += 1

            learner.end_of_iteration(t, done)

            if done:
                episode_ended_at = t
                # bar.update(t, done_at=t)
                # bar.finish(dirty=True)
                break

        sys.stdout.flush()

        loss_string = learner.loss_as_string()

        if (not training_episode_done % (5 * eval_freq)) and configuration.log_results:
            for name, deception_tracker in observation['_deception_tracker'].items():
                detection_points_results[name] = detection_points_results.get(name, [[], [0], []])
                _, name_indptr, _ = detection_points_results[name]
                # if len(deception_tracker.trigger_times):
                detection_points_results[name][1] += [name_indptr[-1] + len(deception_tracker.trigger_times)]
                detection_points_results[name][0] += deception_tracker.trigger_times
                detection_points_results[name][2] += [episode_ended_at if episode_ended_at else iteration_count]

        if loss_string:
            loss_string = f"loss={loss_string}"

        if episode_ended_at:
            print(f"Episode {i_episode} ended at t={episode_ended_at} total_reward {total_reward} with {loss_string}")
        else:
            print(f"Episode {i_episode} stopped at t={iteration_count} total_reward {total_reward} with {loss_string}")

        print_stats(stats)

        all_episodes_sum_rewards.append(sum(all_rewards))
        all_episodes_rewards.append(all_rewards)
        all_episodes_availability.append(all_availability)

        mean_over_window = np.mean(all_episodes_sum_rewards[-mean_reward_window:])
        if best_eval_running_mean < mean_over_window:
            logger.info(f"New best running mean (eval): {mean_over_window}")
            best_eval_running_mean = mean_over_window

            if save_model_filename:
                learner.save(save_model_filename.replace('.tar', f'_eval_steps{training_steps_done + steps_done}.tar'))
                learner.save(save_model_filename.replace('.tar', '_eval_best.tar'))

        if (not training_episode_done % (5 * eval_freq)) and configuration.log_results:
            np.savez(os.path.join(configuration.log_dir, 'training',
                                  f'detection_points_results_eval_trainsteps{training_steps_done}.npz'),
                     **({name + '_indices': np.array(v[0]) for name, v in detection_points_results.items()} |
                        {name + '_indptr': np.array(v[1]) for name, v in detection_points_results.items()} |
                        {name + '_eplength': np.array(v[2]) for name, v in detection_points_results.items()}))

        if configuration.log_results:
            write_to_summary(writer, np.array(all_rewards), epsilon, loss_string, observation, iteration_count, best_eval_running_mean,
                             training_steps_done + steps_done, writer_tag="evaluation")
        length = episode_ended_at if episode_ended_at else iteration_count
        learner.end_of_episode(i_episode=i_episode, t=length)
        # if render:
        #     wrapped_env.render()

    wrapped_env.close()
    logger.info("evaluation ended\n") if configuration.log_results else None

    learner.train()

    return TrainedLearner(
        all_episodes_rewards=all_episodes_rewards,
        all_episodes_availability=all_episodes_availability,
        learner=learner,
        trained_on=cyberbattle_gym_env.name,
        title=plot_title,
        best_running_mean=best_eval_running_mean
    )


def epsilon_greedy_search(
    cyberbattle_gym_env: cyberbattle_env.CyberBattleEnv,
    environment_properties: EnvironmentBounds,
    learner: Learner,
    title: str,
    episode_count: int,
    iteration_count: int,
    epsilon: float,
    epsilon_minimum=0.0,
    epsilon_multdecay: Optional[float] = None,
    epsilon_exponential_decay: Optional[int] = None,
    eval_episode_count: Optional[int] = 0,
    eval_freq: Optional[int] = 5,
    mean_reward_window=10,
    seed=0,
    render=False,
    render_last_episode_rewards_to: Optional[str] = None,
    verbosity: Verbosity = Verbosity.Normal,
    plot_episodes_length=True,
    save_model_filename="",
    only_eval_summary=False
) -> TrainedLearner:
    """Epsilon greedy search for CyberBattle gym environments

    Parameters
    ==========

    - cyberbattle_gym_env -- the CyberBattle environment to train on

    - learner --- the policy learner/exploiter

    - episode_count -- Number of training episodes

    - iteration_count -- Maximum number of iterations in each episode

    - epsilon -- explore vs exploit
        - 0.0 to exploit the learnt policy only without exploration
        - 1.0 to explore purely randomly

    - epsilon_minimum -- epsilon decay clipped at this value.
    Setting this value too close to 0 may leed the search to get stuck.

    - epsilon_decay -- epsilon gets multiplied by this value after each episode

    - epsilon_exponential_decay - if set use exponential decay. The bigger the value
    is, the slower it takes to get from the initial `epsilon` to `epsilon_minimum`.

    - verbosity -- verbosity of the `print` logging

    - render -- render the environment interactively after each episode

    - render_last_episode_rewards_to -- render the environment to the specified file path
    with an index appended to it each time there is a positive reward
    for the last episode only

    - plot_episodes_length -- Plot the graph showing total number of steps by episode
    at th end of the search.

    Note on convergence
    ===================

    Setting 'minimum_espilon' to 0 with an exponential decay <1
    makes the learning converge quickly (loss function getting to 0),
    but that's just a forced convergence, however, since when
    epsilon approaches 0, only the q-values that were explored so
    far get updated and so only that subset of cells from
    the Q-matrix converges.

    """

    # set seeding
    torch.manual_seed(seed)
    random.seed(seed)
    np.random.seed(seed)

    writer = configuration.writer

    print(f"###### {title}\n"
          f"Learning with: episode_count={episode_count},"
          f"iteration_count={iteration_count},"
          f"ϵ={epsilon},"
          f'ϵ_min={epsilon_minimum}, ' +
          (f"ϵ_multdecay={epsilon_multdecay}," if epsilon_multdecay else '') +
          (f"ϵ_expdecay={epsilon_exponential_decay}," if epsilon_exponential_decay else '') +
          f"{learner.parameters_as_string()}")

    initial_epsilon = epsilon

    if configuration.log_results:
        hparams_dict = {"gymid": cyberbattle_gym_env.unwrapped.spec.id,  # env.envs[0].unwrapped.spec.id
                        "date": os.path.normpath(configuration.log_dir).split(os.path.sep)[-1],
                        "agent": learner.name(),  # title: str,
                        "episode_count": episode_count,
                        "iteration_count": iteration_count,
                        "epsilon_minimum": epsilon_minimum,
                        "mean_reward_window": mean_reward_window,
                        "eval_freq": eval_freq,
                        "eval_episode_count": eval_episode_count,
                        "epsilon_exponential_decay": epsilon_exponential_decay,
                        "train_while_exploit": 0}

        # print(learner.parameters_as_string().replace("γ", "gamma").replace("replaymemory", "replay_memory_size").replace(" ", "").replace("\n", "").split(","))

        hparams_dict.update({param_val.split("=")[0]: float(param_val.split("=")[1]) if len(param_val.split("=")) > 1 and
                             float(param_val.split("=")[1]) != round(float(param_val.split("=")[1])) else
                             (int(param_val.split("=")[1]) if len(param_val.split("=")) > 1 else '')
                            for param_val in learner.parameters_as_string().replace("γ",
                                                                                    "gamma").replace("replaymemory",
                                                                                                     "replay_memory_size").replace("\n", "").replace(" ", "").split(",")})
        hparam_domain_discrete = {}
        if 'gamma' in hparams_dict:
            hparam_domain_discrete["gamma"] = [0.015, 0.25, 0.5, 0.8] if '' != hparams_dict.get('gamma', '') else ['']
        if 'train_while_exploit' in hparams_dict:
            hparam_domain_discrete["train_while_exploit"] = [0, 1] if '' != hparams_dict.get('train_while_exploit', '') else ['']
        if 'reward_clip' in hparams_dict:
            hparam_domain_discrete["reward_clip"] = [0, 1] if '' != hparams_dict.get('reward_clip', '') else ['']

        exp, ssi, sei = hparams(hparams_dict,
                                metric_dict={"run_mean": -3000,
                                             "eval_run_mean": -3000,
                                             "loss": sys.float_info.max,
                                             "total_reward": -3000,
                                             },
                                hparam_domain_discrete=hparam_domain_discrete)
        writer.file_writer.add_summary(exp)
        writer.file_writer.add_summary(ssi)
        writer.file_writer.add_summary(sei)

    all_episodes_rewards = []
    all_episodes_sum_rewards = []
    all_episodes_availability = []

    wrapped_env = AgentWrapper(cyberbattle_gym_env,
                               ActionTrackingStateAugmentation(environment_properties, cyberbattle_gym_env.reset()))
    steps_done = 0
    i_episode = 0
    plot_title = (f"{title} (epochs={episode_count}, ϵ={initial_epsilon}, ϵ_min={epsilon_minimum}," +
                  (f"ϵ_multdecay={epsilon_multdecay}," if epsilon_multdecay else '') +
                  (f"ϵ_expdecay={epsilon_exponential_decay}," if epsilon_exponential_decay else '') +
                  learner.parameters_as_string())
    plottraining = PlotTraining(title=plot_title, render_each_episode=render)

    render_file_index = 1
    best_running_mean = -sys.float_info.max
    best_eval_running_mean = -sys.float_info.max
    # detection_tracker_sparce_matrix = np.zer

    detection_points_results = {}

    logger.info('episode_counts ' + str(episode_count))

    # for i_episode in range(1, episode_count + 1):
    while steps_done <= episode_count * iteration_count:
        i_episode += 1

        print(f"  ## Episode: {i_episode}/{episode_count} '{title}' "
              f"ϵ={epsilon:.4f}, "
              f"{learner.parameters_as_string()}")

        observation = wrapped_env.reset()
        total_reward = 0.0
        all_rewards = []
        all_availability = []
        learner.new_episode()

        stats = Stats(exploit=Outcomes(reward=Breakdown(local=0, remote=0, connect=0),
                                       noreward=Breakdown(local=0, remote=0, connect=0)),
                      explore=Outcomes(reward=Breakdown(local=0, remote=0, connect=0),
                                       noreward=Breakdown(local=0, remote=0, connect=0)),
                      exploit_deflected_to_explore=0
                      )

        episode_ended_at = None
        sys.stdout.flush()

        bar = progressbar.ProgressBar(
            widgets=[
                'Episode ',
                f'{i_episode:4}',
                '|Iteration ',
                progressbar.Counter(),
                '|',
                progressbar.Variable(name='steps_done', width=6, precision=6),
                '|',
                progressbar.Variable(name='reward', width=7, precision=5),
                '|',
                progressbar.Variable(name='last_reward_at', width=2),
                '|',
                progressbar.Variable(name='done_at', width=2),
                '|',
                progressbar.Variable(name='loss', width=4, precision=3),
                '|',
                progressbar.Variable(name='epsilon', width=5, precision=3),
                '|',
                progressbar.Variable(name='best_eval_mean', width=6, precision=6),
                '|',
                progressbar.Timer(),
                '|',
                # progressbar.ETA(),
                progressbar.Bar()
            ],
            redirect_stdout=False)

        if epsilon_exponential_decay:  # the less epsilon_exponential_decay, the faster epsilon goes to epsilon_minimum
            epsilon = epsilon_minimum + math.exp(-5. * steps_done /  # min is exp(-5) ~ 0.007 compare to exp(-1) ~ 0.37
                                                 (epsilon_exponential_decay * iteration_count)) * (initial_epsilon - epsilon_minimum)

        for t in bar(range(1, 1 + iteration_count)):

            steps_done += 1

            x = np.random.rand()
            if x <= epsilon:
                action_style, gym_action, action_metadata = learner.explore(wrapped_env)
                logger.info("Choose exploration phase")
            else:
                action_style, gym_action, action_metadata = learner.exploit(wrapped_env, observation)
                if not gym_action:
                    logger.info("Enter exploration phase instead of exploitation")
                    stats['exploit_deflected_to_explore'] += 1
                    _, gym_action, action_metadata = learner.explore(wrapped_env)

            # Take the step
            logger.debug(f"gym_action={gym_action}, action_metadata={action_metadata}") if configuration.log_results else None
            observation, reward, done, info = wrapped_env.step(gym_action)

            action_type = 'exploit' if action_style == 'exploit' else 'explore'
            outcome = 'reward' if reward > 0 else 'noreward'
            if 'local_vulnerability' in gym_action:
                stats[action_type][outcome]['local'] += 1
            elif 'remote_vulnerability' in gym_action:
                stats[action_type][outcome]['remote'] += 1
            else:
                stats[action_type][outcome]['connect'] += 1

            learner.on_step(wrapped_env, observation, reward, done, info, action_metadata)
            assert np.shape(reward) == ()

            all_rewards.append(reward)
            all_availability.append(info['network_availability'])
            total_reward += reward
            bar.update(t, reward=total_reward)
            bar.update(t, epsilon=epsilon)
            bar.update(t, best_eval_mean=best_eval_running_mean)

            if reward > 0:
                bar.update(t, last_reward_at=t)

            if verbosity == Verbosity.Verbose or (verbosity == Verbosity.Normal and reward > 0):
                sign = ['-', '+'][reward > 0]

                print(f"    {sign} t={t} {action_style} r={reward} total_reward:{total_reward} "
                      f"a={action_metadata}-{gym_action} "
                      f"creds={len(observation['credential_cache_matrix'])} "
                      f" {learner.stateaction_as_string(action_metadata)}")

            if i_episode == episode_count \
                    and render_last_episode_rewards_to is not None \
                    and reward > 0:
                fig = cyberbattle_gym_env.render_as_fig()
                fig.write_image(os.path.join(render_last_episode_rewards_to, f"e{i_episode}-s{render_file_index}.png"))
                render_file_index += 1

            learner.end_of_iteration(t, done)

            if done:
                episode_ended_at = t
                bar.update(t, done_at=t)
                bar.update(t, steps_done=steps_done)
                bar.update(t, loss=getattr(learner, 'loss', None))
                bar.finish(dirty=True)
                break

        # Log progressbar to ligfile
        sys.stdout.flush()
        logger.info(str(bar._format_line()))

        loss_string = learner.loss_as_string()

        if configuration.log_results:
            for name, deception_tracker in observation['_deception_tracker'].items():
                detection_points_results[name] = detection_points_results.get(name, [[], [0], []])
                _, name_indptr, _ = detection_points_results[name]
                # if len(deception_tracker.trigger_times):
                detection_points_results[name][1] += [name_indptr[-1] + len(deception_tracker.trigger_times)]
                detection_points_results[name][0] += deception_tracker.trigger_times
                detection_points_results[name][2] += [episode_ended_at if episode_ended_at else iteration_count]

        if loss_string:
            loss_string = f"loss={loss_string}"

        if episode_ended_at:
            print(f"Episode {i_episode} ended at t={episode_ended_at} total_reward {total_reward} with {loss_string}")
        else:
            print(f"Episode {i_episode} stopped at t={iteration_count} total_reward {total_reward} with {loss_string}")

        print_stats(stats)

        # Evaluate model
        if not i_episode % eval_freq:
            logger.info(f"Evaluate network on episode {i_episode} step {steps_done}")
            trained_learner_results = evaluate_model(cyberbattle_gym_env, environment_properties, learner, title, iteration_count, epsilon,
                                                     eval_episode_count, best_eval_running_mean, training_steps_done=steps_done, training_episode_done=i_episode,
                                                     render=True, mean_reward_window=mean_reward_window,
                                                     render_last_episode_rewards_to=None, eval_freq=eval_freq,
                                                     verbosity=Verbosity.Quiet, save_model_filename=save_model_filename)
            best_eval_running_mean = trained_learner_results['best_running_mean']

        all_episodes_sum_rewards.append(sum(all_rewards))
        all_episodes_rewards.append(all_rewards)
        all_episodes_availability.append(all_availability)

        mean_over_window = np.mean(all_episodes_sum_rewards[-mean_reward_window:])
        if best_running_mean < mean_over_window:
            logger.info(f"New best running mean (eval): {mean_over_window}")
            best_running_mean = mean_over_window

            if save_model_filename:
                learner.save(save_model_filename.replace('.tar', f'_steps{steps_done}.tar'))
                learner.save(save_model_filename.replace('.tar', '_best.tar'))

        if configuration.log_results and not only_eval_summary:
            write_to_summary(writer, np.array(all_rewards), epsilon, loss_string, observation, iteration_count, best_running_mean,
                             steps_done)

        length = episode_ended_at if episode_ended_at else iteration_count
        learner.end_of_episode(i_episode=i_episode, t=length)
        if plot_episodes_length:
            plottraining.episode_done(length)
        if render:
            wrapped_env.render()

        if epsilon_multdecay:
            epsilon = max(epsilon_minimum, epsilon * epsilon_multdecay)

        if (not i_episode % (5 * eval_freq)) and configuration.log_results:
            np.savez(os.path.join(configuration.log_dir, 'training', f'detection_points_results_e{i_episode}.npz'),
                     **({name + '_indices': np.array(v[0]) for name, v in detection_points_results.items()} |
                        {name + '_indptr': np.array(v[1]) for name, v in detection_points_results.items()} |
                        {name + '_eplength': np.array(v[2]) for name, v in detection_points_results.items()}))

    if configuration.log_results:
        np.savez(os.path.join(configuration.log_dir, 'training', f'detection_points_results_e{i_episode}.npz'),
                 **({name + '_indices': np.array(v[0]) for name, v in detection_points_results.items()} |
                    {name + '_indptr': np.array(v[1]) for name, v in detection_points_results.items()} |
                    {name + '_eplength': np.array(v[2]) for name, v in detection_points_results.items()}))
        np.savez(os.path.join(configuration.log_dir, 'training', 'detection_points_results.npz'),
                 **({name + '_indices': np.array(v[0]) for name, v in detection_points_results.items()} |
                    {name + '_indptr': np.array(v[1]) for name, v in detection_points_results.items()} |
                    {name + '_eplength': np.array(v[2]) for name, v in detection_points_results.items()}))

    wrapped_env.close()
    logger.info("simulation ended\n") if configuration.log_results else None

    if plot_episodes_length:
        plottraining.plot_end()

    return TrainedLearner(
        all_episodes_rewards=all_episodes_rewards,
        all_episodes_availability=all_episodes_availability,
        learner=learner,
        trained_on=cyberbattle_gym_env.name,
        title=plot_title,
        best_running_mean=best_running_mean
    )


def transfer_learning_evaluation(
    environment_properties: EnvironmentBounds,
    trained_learner: TrainedLearner,
    eval_env: cyberbattle_env.CyberBattleEnv,
    eval_epsilon: float,
    eval_episode_count: int,
    iteration_count: int,
    benchmark_policy: Learner = RandomPolicy(),
    benchmark_training_args=dict(title="Benchmark", epsilon=1.0)
):
    """Evaluated a trained agent on another environment of different size"""

    eval_oneshot_all = epsilon_greedy_search(
        eval_env,
        environment_properties,
        learner=trained_learner['learner'],
        episode_count=eval_episode_count,  # one shot from learnt Q matric
        iteration_count=iteration_count,
        epsilon=eval_epsilon,
        render=False,
        verbosity=Verbosity.Quiet,
        title=f"One shot on {eval_env.name} - Trained on {trained_learner['trained_on']}"
    )

    eval_random = epsilon_greedy_search(
        eval_env,
        environment_properties,
        learner=benchmark_policy,
        episode_count=eval_episode_count,
        iteration_count=iteration_count,
        render=False,
        verbosity=Verbosity.Quiet,
        **benchmark_training_args
    )

    plot_averaged_cummulative_rewards(
        all_runs=[eval_oneshot_all, eval_random],
        title=f"Transfer learning {trained_learner['trained_on']}->{eval_env.name} "
        f'-- max_nodes={environment_properties.maximum_node_count}, '
        f'episodes={eval_episode_count},\n'
        f"{trained_learner['learner'].all_parameters_as_string()}")
