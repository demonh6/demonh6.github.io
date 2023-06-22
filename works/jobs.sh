#!/bin/bash 

./setup -auto -2d Sedov -objdir=simulations/Sedov
# ./setup -auto -2d LaserSlab -objdir=simulations/LaserSlab
# ./setup -auto -2d LaserSlab -nxb=16 -nyb=16 +hdf5typeio species=cham,targ +mtmmmt +laser +uhd3t +mgd mgd_meshgroups=6 -parfile=example.par -objdir=simulations/LaserSlab
# ./setup -auto -2d KHInstab -objdir=simulations/KHInstab
# ./setup -auto -2d SodSpherical -objdir=simulations/SodSpherical
# cd simulations/LaserSlab
cd simulations/Sedov
make -j4
mpirun -np 4 ./flash4






