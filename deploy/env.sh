# JLab paths: /ceph24 is not mounted on ifarm; the real tree is /w/ceph24 (= /work)
export HPC_REMOTE_BASE_TEMPLATE="/w/ceph24/JAM/{username}/fitpack_legacy"
export FITPACK_REMOTE_TEMPLATE="/w/ceph24/JAM/{username}/fitpack_legacy"
export FITPACK_DB_PATH="/home/ec2-user/femtomind/fitpack-workflow/hpc_tasks.db"
export FITPACK_MCP_PATH="/home/ec2-user/femtomind/fitpack-workflow"
export FITPACK_LOCAL_DIR="/home/ec2-user/femtomind/fitpack-workflow"
# plots are computed on the cluster, so this host needs no fitpack physics tree
export FEMTOMIND_PLOT_ON_HPC=1
export FEMTOMIND_HPC_USER=chunyu
