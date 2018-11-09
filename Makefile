
KUBE_NAMESPACE ?= "default"
KUBECTL_VERSION ?= v1.12.1
CI_REGISTRY ?= gitlab.catalyst.net.nz:4567
CI_REPOSITORY ?= piers/k8s-hack
KUBECTL_IMAGE ?= kubectl-delivery:latest
MPIBASE_IMAGE ?= 
REPLICAS ?= 2

# Args for Base Image
UBUNTU_REPOSITORY ?= ubuntu
UBUNTU_TAG ?= 18.04

# Args for SSH Uer embedded in container images
SSH_USER ?= openmpi
SSH_UID ?= 1000
SSH_GID ?= 1000

# mpi
OPENMPI_VERSION ?= 2.1.2
WITH_OPENMPI_BUILD ?= false
KUBECTL_IMAGE ?= kubectl-delivery:latest
MPIBASE_IMAGE ?= mpibase:latest

# define overrides for above variables in here
-include PrivateRules.mak

.DEFAULT: deploy

build_kubectl:
	cd build && \
	docker build \
	  --build-arg 'arg_kubectl_version=$(KUBECTL_VERSION)' \
	  -t kubectl-delivery:latest -f Dockerfile.kubectl .

build_mpibase:
	cd build && \
	docker build \
	  --build-arg UBUNTU_BASED_BASE_IMAGE=$(UBUNTU_REPOSITORY):$(UBUNTU_TAG) \
	  --build-arg OPENMPI_VERSION=$(OPENMPI_VERSION) \
	  --build-arg WITH_OPENMPI_BUILD=$(WITH_OPENMPI_BUILD) \
	  --build-arg SSH_USER=$(SSH_USER) \
	  --build-arg SSH_UID=$(SSH_UID) \
	  --build-arg SSH_GID=$(SSH_GID) \
	  --build-arg 'arg_openmpi_pkg=$(KUBE_OPENMPI_PKG)' \
	  -t mpibase:latest -f Dockerfile.mpibase .

push_kubectl: build_kubectl
	docker tag kubectl-delivery:latest $(KUBECTL_IMAGE)
	docker push $(KUBECTL_IMAGE)

push_mpibase: build_mpibase
	docker tag mpibase:latest $(MPIBASE_IMAGE)
	docker push $(MPIBASE_IMAGE)

build: build_kubectl build_mpibase

push: build push_kubectl push_mpibase

namespace:
	kubectl describe namespace $(KUBE_NAMESPACE) || kubectl create namespace $(KUBE_NAMESPACE)

metacontroller:
	kubectl apply -f https://raw.githubusercontent.com/GoogleCloudPlatform/metacontroller/master/manifests/metacontroller-rbac.yaml
	kubectl apply -f https://raw.githubusercontent.com/GoogleCloudPlatform/metacontroller/master/manifests/metacontroller.yaml

deploy: namespace
	kubectl create configmap mpi-controller -n metacontroller --from-file=sync.py
	KUBECTL_IMAGE=$(KUBECTL_IMAGE) \
	 envsubst < deploy/mpi-controller.yaml | kubectl apply -n metacontroller -f -

test: regisry-creds
	MPIBASE_IMAGE=$(MPIBASE_IMAGE) \
	REPLICAS=$(REPLICAS) \
	 envsubst < mpi-test.yaml | kubectl apply -f - -n $(KUBE_NAMESPACE)

test-results:
	kubectl get pods -l job-name=mpioperator-test-mpi-launcher
	kubectl get pods -l job-name=mpioperator-test-mpi-launcher | \
	grep Completed | cut -f1 -d" " | xargs kubectl logs || true

test-clean:
	kubectl delete -f mpi-test.yaml -n $(KUBE_NAMESPACE) || true

regisry-creds: namespace
	@kubectl create secret -n $(KUBE_NAMESPACE) \
	  docker-registry $(PULL_SECRET) \
	 --docker-server=$(CI_REGISTRY) \
	 --docker-username=$(GITLAB_USER) \
	 --docker-password=$(REGISTRY_PASSWORD) \
	 --docker-email=$(GITLAB_USER_EMAIL) \
	-o yaml --dry-run | kubectl replace -n $(KUBE_NAMESPACE) --force -f -

fixdns:
	# remove 'loop' - https://github.com/coredns/coredns/issues/2087
	kubectl -n kube-system edit configmap coredns

clean:
	kubectl delete -f mpi-test.yaml -n $(KUBE_NAMESPACE) || true
	sleep 3
	kubectl delete pods -l app=mpi-controller || true
	KUBECTL_IMAGE=$(KUBECTL_IMAGE) \
	 envsubst < deploy/mpi-controller.yaml | kubectl delete -n metacontroller -f - || true
	kubectl delete configmap mpi-controller -n metacontroller || true

redeploy: clean deploy

