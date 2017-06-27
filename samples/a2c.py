#!/usr/bin/env python
import argparse
import numpy as np

import torch
import torch.nn as nn
import torch.nn.functional as F
import torch.optim as optim
from torch.autograd import Variable

import ptan
from ptan.common import runfile, env_params

import gym

from bokeh.plotting import figure, output_file, show

GAMMA = 0.99


class Model(nn.Module):
    def __init__(self, n_actions, input_len):
        super(Model, self).__init__()

        self.fc1 = nn.Linear(input_len, 50)
        self.fc2 = nn.Linear(50, 50)
        self.out_policy = nn.Linear(50, n_actions)
        self.out_value = nn.Linear(50, 1)

    def forward(self, x):
        x = self.fc1(x)
        x = F.relu(x)
        x = self.fc2(x)
        x = F.relu(x)
        policy = F.softmax(self.out_policy(x))
        value = self.out_value(x)
        return policy, value


def a3c_actor_wrapper(model):
    def _wrap(x):
        x = model(x)
        return x[0]
    return _wrap


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("-r", "--runfile", required=True, help="Name of the runfile to use")
    parser.add_argument("-m", "--monitor", help="Use monitor and save it's data into given dir")
    args = parser.parse_args()

    run = runfile.RunFile(args.runfile)

    cuda_enabled = run.getboolean("defaults", "cuda", fallback=False)
    env = gym.make(run.get("defaults", "env")).env
    if args.monitor:
        env = gym.wrappers.Monitor(env, args.monitor)

    params = env_params.EnvParams.from_env(env)
    env_params.register(params)

    # model returns probability of actions
    model = Model(params.n_actions, params.state_shape[0])
    if cuda_enabled:
        model.cuda()

    agent = ptan.agent.PolicyAgent(a3c_actor_wrapper(model))
    exp_source = ptan.experience.ExperienceSource(env=env, agent=agent, steps_count=run.getint("defaults", "n_steps"))

    optimizer = optim.Adam(model.parameters(), lr=run.getfloat("learning", "lr"))

    batch = []

    def calc_loss(batch):
        """
        Calculate loss from experience batch
        :param batch: list of experience entries
        :return: loss variable
        """
        # quite inefficient way, should be reordered to minimize amount of model() calls
        result = Variable(torch.FloatTensor(1).zero_())

        for exps in batch:
            v = Variable(torch.from_numpy(np.array([exps[0].state, exps[-1].state], dtype=np.float32)))
            policy_s, value_s = model(v)
            t_policy_s = policy_s[0]
            t_value_s = value_s[0]
            t_value_last_s = value_s[1]
            R = 0.0 if exps[-1].done else t_value_last_s.data.cpu().numpy()[0]
            for exp in reversed(exps):
                R *= GAMMA
                R += exp.reward
            advantage = R - t_value_s.data.cpu().numpy()[0]
            # policy loss part
            result += -t_policy_s.log()[exps[0].action] * advantage
            # value loss part
            result += (t_value_s - R) ** 2
            # TODO: entropy loss

        return result / len(batch)

    losses = []
    rewards = []
    iter_idx = 0

    graph_data = {
        'full_loss': [],
        'rewards': [],
    }

    for exp in exp_source:
        batch.append(exp)
        if len(batch) < run.getint("learning", "batch_size"):
            continue

        # handle batch with experience
        optimizer.zero_grad()
        loss = calc_loss(batch)
        loss.backward()
        optimizer.step()
        f_loss = loss.data.cpu().numpy()[0]
        batch = []

        iter_idx += 1
        losses.append(f_loss)
        new_rewards = exp_source.pop_total_rewards()
        rewards.extend(new_rewards)
        losses = losses[-10:]
        rewards = rewards[-10:]

        print("%d: mean_loss=%.3f, mean_reward=%.3f" % (iter_idx, np.mean(losses), np.mean(rewards)))
        graph_data['full_loss'].append(f_loss)
        graph_data['rewards'].extend(new_rewards)

        if np.mean(rewards) > 300:
            break

    # plot charts
    output_file("a2c.html")

    f = figure(title="Full loss")
    f.line(range(iter_idx), graph_data['full_loss'])
    show(f)