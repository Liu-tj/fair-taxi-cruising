from src.Solvers.TestingSolver import *
import numpy as np

class NoSolver(TestingSolver):
    def __init__(self, **params):
        super().__init__(**params)

    def train(self):
        pass

    def load(self):
        pass

    def save(self):
        pass

    def predict(self, observation, info):
        action = np.zeros(self.testing_env.action_space.shape)
        wl = len(self.testing_env.world)
        last_ind = action.shape[0] // wl
        for i in range(wl):
            action[i*last_ind-1] = 1
        return action
