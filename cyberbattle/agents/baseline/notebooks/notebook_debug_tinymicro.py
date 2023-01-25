# ---
# jupyter:
#   jupytext:
#     cell_metadata_filter: tags,title,-all
#     cell_metadata_json: true
#     text_representation:
#       extension: .py
#       format_name: percent
#       format_version: '1.3'
#       jupytext_version: 1.6.0
#     kernelspec:
#       display_name: python3
#       language: python
#       name: python3
# ---

# %%
# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

"""Notebook used for debugging purpose to train the
the DQL agent and then run it one step at a time.
"""

# pylint: disable=invalid-name

# %%
import os
import logging
import gym
import datetime
from IPython.display import display
import cyberbattle.agents.baseline.learner as learner
from cyberbattle.agents.baseline.agent_wrapper import ActionTrackingStateAugmentation, AgentWrapper, Verbosity
import cyberbattle.agents.baseline.agent_wrapper as w
from cyberbattle.simulation.config import logger, configuration
import cyberbattle.agents.baseline.agent_dql as dqla


import pandas as pd
from dotenv import load_dotenv

load_dotenv()


# # torch.cuda.set_device('cuda:3')
# log_level_dict = {"info": logging.INFO, "error": logging.ERROR, "debug": logging.DEBUG, "warn": logging.WARN, }
# logging.basicConfig(level=log_level_dict[os.environ["LOG_LEVEL"]],
#                     format="[%(asctime)s] %(levelname)s: %(message)s", datefmt='%H:%M:%S',
#                     handlers=[logging.StreamHandler(sys.stdout)])  # + ([logging.FileHandler(os.path.join(log_dir, 'logfile.txt'))] if log_results else []))

# logger = logging.getLogger()
# logger.setLevel(logging.ERROR)


# %% tags=['parameters']
max_episode_steps = 50
log_results = os.getenv("LOG_RESULTS", 'False').lower() in ('true', '1', 't')
gymid = os.getenv("GYMID", 'CyberBattleTinyMicro-v0')
log_level = os.getenv('LOG_LEVEL', "info")
iteration_count = None
eval_episode_count = int(os.getenv('EVAL_EPISODE_COUNT', 0))
training_episode_count = None
train_while_exploit = False
exploit_train = "exploit_train"   # "exploit_manual"

log_dir = 'logs/exper/' + "notebook_debug_tinymicro"
# convert the datetime object to string of specific format
log_level = os.getenv('LOG_LEVEL', "info")
checkpoint_name = None if os.getenv('CHECKPOINT', 'None').lower() in ('none') else os.environ['CHECKPOINT'].lower()
iteration_count = None
checkpoint_date = None

# %%
iteration_count = max_episode_steps if iteration_count is None else iteration_count
os.environ['TRAINING_EPISODE_COUNT'] = os.getenv('TRAINING_EPISODE_COUNT', 3000) if training_episode_count is None else training_episode_count
training_episode_count = int(os.environ['TRAINING_EPISODE_COUNT'])

checkpoint_date = checkpoint_date if checkpoint_date else os.getenv('CHECKPOINT_DATE', '20230124_085534')
datetime_str = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
checkpoint_dir = os.path.join("logs/exper/" + "notebook_dql_debug_with_tinymicro", gymid, checkpoint_date)
log_dir = os.path.join(log_dir, gymid, checkpoint_date)
os.environ['LOG_DIR'] = log_dir
os.environ['LOG_RESULTS'] = str(log_results).lower()


os.makedirs(log_dir, exist_ok=True) if log_results else ''
configuration.update_globals(log_dir, gymid, log_level, log_results)
configuration.update_logger()

# if os.environ['RUN_IN_SILENT_MODE'] in ['true']:
#     f = open(os.devnull, 'w')
#     sys.stdout = f


# %%
# Load the gym environment

ctf_env = gym.make(gymid)
ctf_env.spec.max_episode_steps = max_episode_steps

# %%
iteration_count = ctf_env.spec.max_episode_steps if iteration_count is None else iteration_count
max_steps = iteration_count
verbosity = Verbosity.Normal

# %%
logger.setLevel(logging.INFO)

logger.info("Logging into directory " + log_dir)
logger.info("")
# %%

ep = w.EnvironmentBounds.of_identifiers(
    maximum_node_count=ctf_env.bounds.maximum_node_count,  # either we identify from configuration, or by ourselves
    maximum_total_credentials=1,
    identifiers=ctf_env.identifiers
)

ctf_env = gym.make(gymid, env_bounds=ep)
ctf_env.spec.max_episode_steps = max_episode_steps
current_o = ctf_env.reset()
wrapped_env = AgentWrapper(ctf_env, ActionTrackingStateAugmentation(ep, current_o))
# %%
# !!! Track Profiles data yourself and make EXACT string for profiles, etc. do not forget about found properties, like id, roles.
# Choosing ip.local means
# 1) we found ip.local and registered as self.__ip_local flag;
# 2) the profiles are still writen with ip = None, but you can use ip.local in profile_str to turn on/off SSRF action
manual_commands = [
    {'local': ['client_browser', 'ScanPageSource']},
    {'local': ['client_browser', 'ScanBlockRegister']},
    {'remote': ['client_browser', 'POST_/v2/register', "username.NoAuth", ""]},
    {'remote': ['client_browser', 'GET_/v2/calendar', "username.patient&id.UUIDfake", ""]},
    {'remote': ['client_browser', 'GET_/v2/users', "username.LisaGWhite", "username"]},
    {'remote': ['client_browser', 'GET_/v2/messages', "username.LisaGWhite&id.994D5244&roles.isDoctor", ""]},
    {'remote': ['client_browser', 'GET_/v2/users', "username.MarioDFiles", "username"]},
    {'remote': ['client_browser', 'GET_/v2/messages', "username.MarioDFiles&id.F5BCFE9D&roles.isDoctor", ""]},
    # {'remote': ['client_browser', 'GET_/v2/users', "username.LisaGWhite&id.994D5244&roles.isDoctor", ""]}, # redundant actions
    {'remote': ['client_browser', 'GET_/v2/users', "username.LisaGWhite&id.994D5244&roles.isDoctor&ip.local", ""]},
    # {'remote': ['client_browser', 'GET_/v2/users', "username.MarioDFiles&id.F5BCFE9D&roles.isDoctor&ip.local", "username"]}, # redundant actions
    # {'remote': ['client_browser', 'GET_/v2/messages', "username.LisaGWhite&id.994D5244&roles.isDoctor&ip.local", ""]}, # redundant actions
    {'remote': ['client_browser', 'GET_/v2/documents', "username.JamesMPaterson&id.68097B9D&roles.isChemist", ""]},
]

if checkpoint_name is not None:
    learning_rate = 0.01  # 0.01
    gamma = 0.015  # 0.015
    DQL_agent = dqla.DeepQLearnerPolicy(
        ep=ep,
        gamma=gamma,
        replay_memory_size=10000,
        target_update=5,
        batch_size=512,
        learning_rate=learning_rate  # torch default learning rate is 1e-2
    )
    if checkpoint_name == 'best':
        DQL_agent.load_best(os.path.join(checkpoint_dir, 'training'))
    elif checkpoint_name.isnumeric():
        DQL_agent.load(os.path.join(checkpoint_dir, 'training',
                                    f'exploit_train__trainepisodes{training_episode_count}_best_modelevaluation_stepsdone_{checkpoint_name}.tar'))
    else:
        raise ValueError(f"Checkpoint name {checkpoint_name} is not none, best or stepsdone number")
    DQL_agent.train_while_exploit = False
    DQL_agent.policy_net.eval()

# %%
h = []
done = False
total_reward = 0
for i in range(max(max_steps, len(manual_commands))):
    wrapped_env.render(mode='rgb_array', filename=None if not log_results else
                       os.path.join(log_dir, f'{exploit_train}_{train_while_exploit*"train_while_exploit"}_step{i}_checkpoint{checkpoint_name}_episodes_output_result.png'))
    logger.info("")
    if done:
        break
    # run the suggested action or exploited action
    action_style, next_action, _ = DQL_agent.exploit(wrapped_env, current_o) if checkpoint_name else  \
        wrapped_env.pretty_print_to_internal_action(manual_commands[i])

    if next_action is None:
        logger.info(f"Inference ended with error: next action == None, returned with aciton_style {action_style}")
        break
    current_o, reward, done, info = wrapped_env.step(next_action)
    total_reward += reward
    action_str, reward_str = wrapped_env.internal_action_to_pretty_print(next_action, output_reward_str=True)
    h.append((i,  # wrapped_env.get_explored_network_node_properties_bitmap_as_numpy(current_o),
              reward, total_reward,
              action_str, action_style, info['precondition_str'], info['profile_str'], info["reward_string"]))

df = pd.DataFrame(h, columns=["Step", "Reward", "Cumulative Reward", "Next action", "Processed by", "Precondition", "Profile", "Reward string"])
df.set_index("Step", inplace=True)
pd.set_option("max_colwidth", 80)
if log_results:
    display(df)

if log_results:
    os.makedirs(log_dir, exist_ok=True)
    df.to_csv(os.path.join(log_dir, f'{exploit_train}_{train_while_exploit*"train_while_exploit"}_step{i}_checkpoint{checkpoint_name}_episodes_actions.csv'))  # ,
    # index=False)
print(f'len: {len(h)}, cumulative reward: {total_reward}')

# %%
wrapped_env.render(mode='rgb_array' if not log_results else 'human', filename=None if not log_results else
                   os.path.join(log_dir, f'{exploit_train}_{train_while_exploit*"train_while_exploit"}_checkpoint{checkpoint_name}_episodes_discovered_network.png'))
