import warnings
warnings.filterwarnings("ignore")

import sys, os
from src.Expert.Experiment import *
import json
import datetime

def run_experiment(dag):
	with open(os.path.join(os.path.dirname(os.path.realpath(__file__)),'..','dags', dag + '.json'),'r') as f:
		params = json.load(f)
	params['tag'] = dag
	print("Experiment {} started at {}".format(dag, datetime.datetime.now()))
	experiment = Experiment(params)
	experiment.run()


if __name__ == "__main__":
	scriptname = sys.argv[1]
	run_experiment(scriptname)
