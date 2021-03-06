import os, sys
import imageio
import os
import numpy as np
import shutil
import gym
import time
import logging

from stable_baselines import A2C, PPO2, ACKTR
from stable_baselines.common.vec_env import SubprocVecEnv, VecNormalize
from stable_baselines.common.policies import MlpPolicy, MlpLstmPolicy
from stable_baselines.common import set_global_seeds

from src.Solvers.TestingSolver import TestingSolver
from src.Generator import Generator
from src.Expert.FileManager import FileManager
from src.Simulator.simulator.oenvs import CityReal
from src.Expert.ParameterManager import ParameterManager

call = 0

class GymSolver(TestingSolver):
    def __init__(self, **params):
        super().__init__(**params)
        self.Model = PPO2
        self.solver_signature = "gym_" + ParameterManager.get_param_footprint(self.get_footprint_params())

        # parameters from our config, not the original one
        self.days = self.params['dataset']["days"]
        env_id = "TaxiEnv-v01"
        self.env_params = self.load_env_params()

        seed = np.random.randint(1,10000)
        self.log['seed'] = seed

        if self.params.get("lstm", 0) == 1:
            Policy = MlpLstmPolicy
            nminibatches = 1
            num_cpu = 1 # One current limitation of recurrent policies is that you must test them with the same number of environments they have been trained on.
        else:
            Policy = MlpPolicy
            nminibatches = 4
            num_cpu = self.params['num_cpu']
        # Create the vectorized environment
        self.train_env = SubprocVecEnv([self.make_env(env_id, i, seed+i, self.env_params) for i in range(num_cpu)])

        self.train_env = VecNormalize(self.train_env, norm_obs=False, norm_reward=False)

        # self.model = self.Model(Policy, self.train_env, verbose=0, nminibatches=nminibatches, tensorboard_log=os.path.join(self.dpath,self.solver_signature))
                                # minibatches are important, and no parallelism
                                #n_steps=self.params['dataset']['time_periods']+1,
        self.model = self.Model(Policy, self.train_env, verbose=0, nminibatches=4, tensorboard_log=os.path.join(self.dpath,self.solver_signature), n_steps=self.params['dataset']['time_periods']+1)

    def get_footprint_params(self):
        footprint_params = {
            "n_intervals": self.time_periods,
            "wc": self.params["wc"],
            "count_neighbors": self.params['count_neighbors'] == 1,
            "weight_poorest": self.params['weight_poorest'] == 1,
            "normalize_rewards": self.params['normalize_rewards'] == 1,
            "minimum_reward": self.params['minimum_reward'] == 1,
            "include_income_to_observation": self.params['include_income_to_observation'] == 1,
            "poorest_first": self.params.get("poorest_first", 0) == 1
        }
        if self.params.get("lstm", 0) == 1:
            footprint_params['lstm'] = True
        return footprint_params

    def load_env_params(self):
        '''
        load complete dataset

        note that orders are merged into a single day, and then sampled out of there
        '''
        dataset_params = self.params['dataset']
        gen = Generator(self.params['tag'], dataset_params)
        world, idle_driver_locations, real_orders, onoff_driver_locations, random_average, dist = gen.load_complete_set(dataset_id=self.params['dataset']['dataset_id'])
        params = {
            "world": world,
            "orders": real_orders,
            "order_sampling_rate": 1./self.days*self.params['dataset']['order_sampling_multiplier'],
            "drivers_per_node": idle_driver_locations[0,:],
            "n_intervals": self.params['dataset']['time_periods'],
            "wc": self.params['wc'],
            "count_neighbors": self.params['count_neighbors'] == 1,
            "weight_poorest": self.params['weight_poorest'] == 1,
            "normalize_rewards": self.params['normalize_rewards'] == 1,
            "minimum_reward": self.params['minimum_reward'] == 1,
            "include_income_to_observation": self.params['include_income_to_observation'] == 1,
            "poorest_first": self.params.get("poorest_first", 0) == 1
        }

        return params


    def make_env(self, env_id, rank, seed=0, env_params={}):
        """
        Utility function for multiprocessed env.

        :param env_id: (str) the environment ID
        :param num_env: (int) the number of environments you wish to have in subprocesses
        :param seed: (int) the inital seed for RNG
        :param rank: (int) index of the subprocess
        """
        def _init():
            gym.envs.register(
                id=env_id,
                entry_point='gym_taxi.envs:TaxiEnv',
                kwargs=env_params
            ) # must be in make_env because otherwise doesn't work
            env = gym.make(env_id)
            env.seed(seed + rank)
            return env
        set_global_seeds(seed)
        return _init

    def get_callback(self):
        """
        Callback called at each step (for DQN an others) or after n steps (see ACER or PPO2)
        :param _locals: (dict)
        :param _globals: (dict)
        """
        global call
        call = 0
        self.log['iterations_stats'] = {}
        def callback(_locals, _globals):
            global call
            if call % 4 == 0:
                stats = self.do_test_iteration()
                self.log['iterations_stats'][str(len(self.log['iterations_stats']))] = stats
            call += 1
            return True

        def no_callback(_locals, _globals):
            return True

        if self.params.get("callback", 0) == 1:
            return callback
        else:
            return no_callback

    def train(self):
        t = time.time()
        _ = self.get_callback()
        self.model.learn(total_timesteps=self.params['training_iterations'], callback=self.get_callback())
        self.log['training_time'] = time.time() - t

    def predict(self, state, info, nn_state = None):
        # return bunch of actions given learned state-action for singular taxi
        # state is the one returned by testing environment (multitaxi), and action returned should be for that too
        actions = []
        for n in self.world.nodes():
            onehot_nodeid = np.zeros(len(self.world))
            onehot_nodeid[n] = 1
            if self.params['include_income_to_observation'] == 1:
                assert self.testing_env.include_income_to_observation
                positions_for_income = 3*len(self.world)
                assert state[:-positions_for_income].shape == ((2*len(self.world)+self.time_periods),)
                assert state[-positions_for_income:].shape[0] == positions_for_income
                assert self.train_env.observation_space.shape == (3*len(self.world)+self.time_periods+3,), (self.train_env.observation_space.shape, (3*len(self.world)+self.time_periods+3,))
                if -positions_for_income+3*n+3 == 0:
                    last = state[-positions_for_income+3*n:]
                else:
                    last = state[-positions_for_income+3*n:-positions_for_income+3*n+3]
                obs = np.concatenate((state[:-positions_for_income], onehot_nodeid, last))
                assert obs.shape[0] == 3*len(self.world)+self.time_periods+3
            else:
                assert not self.testing_env.include_income_to_observation
                assert state.shape == (2*len(self.world)+self.time_periods,), (state.shape, (2*len(self.world)+self.time_periods,))
                obs = np.concatenate((state, onehot_nodeid))
                assert obs.shape[0] == 3*len(self.world)+self.time_periods

            if self.params.get("lstm", 0) == 1:
                raise NotImplementedError("The problem is how to combine last states of NN from different graph nodes")
                action, _states = self.model.predict(obs, state=nn_state, mask=done)
            else:
                action, _states = self.model.predict(obs)
            actions.append(action)
        actions = np.concatenate(actions)
        return actions

    def load(self):
        self.model = self.Model.load(os.path.join(self.dpath, "{}_taxi_gym_model".format(self.solver_signature)))

    def save(self):
        self.model.save(os.path.join(self.dpath, "{}_taxi_gym_model".format(self.solver_signature)))
