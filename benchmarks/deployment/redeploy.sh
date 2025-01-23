#!/bin/bash
set +x
TAG="paper"
AIBRIX_DIR=/root/aibrix
DEPLOYMENT_PATH=$AIBRIX_DIR/config/overlays/vke-ipv4/default/kustomization.yaml 
export KUBECONFIG=/root/.kube/config-vke
envsubst < kustomization.yaml.template > $DEPLOYMENT_PATH

cd $AIBRIX_DIR

make undeploy-vke-ipv4
make deploy-vke-ipv4