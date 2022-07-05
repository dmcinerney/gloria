module load anaconda3/3.7
module load cuda/10.2
module load discovery/2019-02-21
#source activate jpt
source activate gloria2
ssh login-00 -f -N -T -R 8903:localhost:8903
jupyter notebook --port 8903
