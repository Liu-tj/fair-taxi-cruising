import pandas as pd
import numpy as np
from collections import defaultdict
from tqdm import tqdm
import logging
import signal
from subprocess import Popen, PIPE
from multiprocessing.pool import ThreadPool
import threading
import time
import uuid

import sys, os
from src.Expert.FileManager import FileManager
from src.Expert.ParameterManager import ParameterManager
from src.Generator import Generator

from src.Solvers.Solver import *
from src.Solvers.DummySolver import *
from src.Solvers.NoSolver import *
from src.Solvers.DiffSolver import *
from src.Solvers.cA2CSolver import *
from src.Solvers.RwSolver import *
from src.Solvers.ValIterSolver import *
from src.Solvers.RobustValIterSolver import *
from src.Solvers.LinearSolver import *
from src.Solvers.OrigNoSolver import *
from src.Solvers.OrigValIterSolver import *
from src.Solvers.OrigA2CSolver import *
from src.Solvers.GymSolver import *
from src.Solvers.RobustGymSolver import *
from src.Solvers.OrientedSolver import *

from src.Expert.MongoDatabase import *
from src.Expert.ParameterManager import *

# set up logging to file - see previous section for more details
logging.basicConfig(level=logging.DEBUG,
                    format='%(asctime)-3s :: %(levelname)-3s :: %(threadName)s :: %(message)s',
                    datefmt='%m-%d %H:%M',
                    filename=os.path.join(FileManager.get_root_path(),'MacaoLog.log'),
                    filemode='a')
# define a Handler which writes warning messages or higher to the sys.stderr
console = logging.StreamHandler()
console.setLevel(logging.INFO)
# set a format which is simpler for console use
formatter = logging.Formatter('%(asctime)-3s %(levelname)-4s %(relativeCreated)6d %(threadName)s: %(message)s')
# tell the handler to use this format
console.setFormatter(formatter)
# add the handler to the root logger
logging.getLogger('').addHandler(console)

class SolverTimeoutException(Exception):
    pass
def handler(signum, frame):
    raise SolverTimeoutException()

class Experiment:
    '''
    All parameters, including paths of datasets, are loaded to and from MongoDB in this class.
    Scripts and classes are responsible for passing necessary information to the next script, and for saving data.
    '''
    WORKERS_NUM = 1

    def __init__(self, parameters = {}):
        logging.info("\n============================ INIT EXPERIMENT ===============================\n")
        self.pm = ParameterManager(parameters)
        self.tag = self.pm.get('tag')

        self.db_wrapper = MongoDatabase(self.tag)
        self.db = self.db_wrapper.db
        self.fm = FileManager(self.tag)
        if self.pm.get('full_rerun') == 1:
            self.clear()
            self.fm.clean_data_path()
            parameters = self.pm.get_all_params()
            self.db.parameters.insert_one(parameters)

    def clear(self):
        logging.info("Cleaning data path")
        self.fm.clean_data_path()
        self.db.parameters.delete_many({'tag': self.tag})

    def generate_datasets(self, force=False):
        if force:
            self.db.dataset.delete_many({'tag': self.tag})
        if self.db.dataset.find_one({'tag': self.tag}) != None:
            logging.info("Dataset for {} has been found".format(self.tag))
            return 0
        total_datasets = list(self.pm.get_data_param_sets())
        if len(total_datasets) == 0:
            raise Exception("No datasets generated, bad parameter values")
        generated = 0
        for p in total_datasets:
            gen = Generator(self.tag, p)
            dataset_info = gen.generate()
            self.db.dataset.insert_one(dataset_info)
            generated += 1
        return generated

    def run_parallel(self):
        logging.info("..... Starting parallel execution of {} tasks .............".format(len(self.all_solvers)))
        results = []

        t = tqdm(total=len(self.all_solvers), desc='Running Solvers')
        def callbackfunct(smth):
            t.update()
            t.refresh()
        def handle_error(error):
            # if there is a solver error, then it falls WITHIN a thread, not here.
            # Here it falls only if there is a bug with ParallelWrapper itself
            logging.error(error)

        tp = ThreadPool(Experiment.WORKERS_NUM)
        for solver_params in self.all_solvers:
            results.append(tp.apply_async(Experiment.run_solver, (solver_params, self.db),
                                            callback=callbackfunct, error_callback=handle_error))
        tp.close()
        tp.join()
        t.close()

    @staticmethod
    def run_solver(solver_params, db_client):
        solver_name = solver_params["solver"]
        Solver = eval(solver_name + "Solver")
        z = dict(solver_params)
        solver = Solver(**z)
        random_seed = time.time()
        solver.seed(int(random_seed))

        if threading.current_thread().name == 'MainThread':
            logging.info("Starting {} of Solver {}".format(solver_params["mode"], solver_name))
            solver.verbose = True

        result = solver.run()
        signal.alarm(0)
        try:
            db_client.solution.insert_one(result)
        except Exception as e:
            logging.error("Failed to save result: {}, Result: {}".format(e, result))

    def generate_solver_params(self, mode):
        if mode == "Train":
            q = {"tag": self.tag}
            total_datasets = self.db.dataset.count_documents(q)
            if (total_datasets == 0):
                raise Exception("No datasets found for the tag")

            for d in self.db.dataset.find(q):
                for solver_params in self.pm.get_solvers_params():
                    if solver_params.get("rerun",0) == 1:
                        self.db.solution.delete_many({"tag": self.tag, "solver": solver_params['solver']})
                        logging.info("Solutions for solver {} removed.".format(solver_params['solver']))

                    all_params = deepcopy(solver_params)
                    all_params['dataset'] = d
                    all_params['mode'] = mode
                    all_params['footprint'] = ParameterManager.get_param_footprint(solver_params)
                    all_params['tag'] = self.tag
                    if self.db.solution.find_one(all_params) != None:
                       logging.info("{} of {} for {} exists.".format(mode, solver_params['solver'], self.tag))
                       continue
                    if "rerun" in all_params:
                        del all_params['rerun']
                    all_params['trained_model_id'] = str(uuid.uuid4())

                    yield all_params

        if mode == "Test":
            q = {"tag": self.tag, "mode": "Train"}
            total_datasets = self.db.solution.count_documents(q)
            if (total_datasets == 0):
                raise Exception("No learned models found for the tag")

            for d in self.db.solution.find(q):
                model_id = d['trained_model_id']

                if self.db.solution.find_one({'trained_model_id': model_id, "mode": "Test"}) != None:
                   logging.info("{} of {} for {} exists.".format(mode, d['solver'], self.tag))
                   continue

                all_params = d
                del all_params["_id"]
                all_params['mode'] = "Test"
                yield all_params

    def run_solvers(self, mode, force=False):
        if force:
            if mode == "Train":
                self.db.solution.delete_many({'tag': self.tag}) #delete both
            else:
                self.db.solution.delete_many({'tag': self.tag, "mode": mode})

        self.all_solvers = []
        for all_params in self.generate_solver_params(mode):
            # print(all_params)
            if Experiment.WORKERS_NUM == 1:
                self.run_solver(all_params, self.db)
            else:
                self.all_solvers.append(all_params)

        if len(self.all_solvers) > 0:
            self.run_parallel()

    def run(self):
        generated = self.generate_datasets(force = self.pm.get('full_rerun') == 1)
        logging.info("{} datasets generated".format(generated))
        self.run_solvers("Train", force = self.pm.get('full_rerun') == 1)
        logging.info("All methods trained")
        self.run_solvers("Test", force = self.pm.get('full_rerun') == 1)
        logging.info("Experiment {} completed".format(self.tag))
