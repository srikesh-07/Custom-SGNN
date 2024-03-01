#!/bin/bash

python main.py --dataset PTC --K 72 --alpha 0.3 --mu1 1.5 --mu2 1.5
python main.py --dataset PROTEINS --K 251 --alpha 0.15 --mu1 2 --mu2 2
python main.py --dataset DD --K 228 --alpha 0.1 --mu1 0.5 --mu2 0.5
python main.py --dataset IMDBBINARY --K 205 --alpha 0.15 --mu1 1 --mu2 1
python main.py --dataset FRANK --K 922 --alpha 0.1 --mu1 2 --mu2 0

python main.py --dataset PTC --K 72 --alpha 0.3 --mu1 1.5 --mu2 1.5 --size_strat
python main.py --dataset PROTEINS --K 251 --alpha 0.15 --mu1 2 --mu2 2 --size_strat
python main.py --dataset DD --K 228 --alpha 0.1 --mu1 0.5 --mu2 0.5 --size_strat
python main.py --dataset IMDBBINARY --K 205 --alpha 0.15 --mu1 1 --mu2 1 --size_strat
python main.py --dataset FRANK --K 922 --alpha 0.1 --mu1 2 --mu2 0 --size_strat
