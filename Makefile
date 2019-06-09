
KUBE_NAMESPACE ?= "default"
KUBECTL_VERSION ?= 1.14.1
HELM_VERSION ?= v2.14.0
HELM_CHART = mpi-operator
HELM_RELEASE ?= test
OPERATOR_NAMESPACE ?= metacontroller
CI_REGISTRY ?= gitlab.catalyst.net.nz:4567
CI_REPOSITORY ?= piers/k8s-hack
REPLICAS ?= 2

# Args for Base Image
UBUNTU_BASE_IMAGE ?= ubuntu:18.04

# mpi
OPENMPI_VERSION ?= 2.1.2
WITH_OPENMPI_BUILD ?= false
KUBECTL_BASE_IMAGE_VERSION ?= 3.9
KUBECTL_IMAGE ?= piersharding/kubectl-delivery
MPIBASE_TAG ?= $(shell echo $(UBUNTU_BASE_IMAGE) | sed 's/\://')
MPIBASE_IMAGE ?= piersharding/mpibase
MYHOST := $(shell hostname)

.PHONY: k8s show lint deploy delete logs describe namespace test clean metalogs help
.DEFAULT_GOAL := help

# define overrides for above variables in here
-include PrivateRules.mak

k8s: ## Which kubernetes are we connected to
	@echo "Kubernetes cluster-info:"
	@kubectl cluster-info
	@echo ""
	@echo "kubectl version:"
	@kubectl version
	@echo ""
	@echo "Helm version:"
	@helm version --client
	@echo ""
	@echo "Helm plugins:"
	@helm plugin list

check: ## Lint check Operator
	pylint3 charts/mpi-operator/configs/sync.py
	flake8 charts/mpi-operator/configs/sync.py

build_kubectl:
	cd build && \
	docker build \
	  --build-arg 'KUBECTL_VERSION=$(KUBECTL_VERSION)' \
		--build-arg 'BASE_IMAGE_VERSION=$(KUBECTL_BASE_IMAGE_VERSION)' \
	  -t kubectl-delivery:latest -f Dockerfile.kubectl .

build_mpibase:
	cd build && \
	docker build \
	  --build-arg UBUNTU_BASE_IMAGE=$(UBUNTU_BASE_IMAGE) \
	  --build-arg OPENMPI_VERSION=$(OPENMPI_VERSION) \
	  --build-arg WITH_OPENMPI_BUILD=$(WITH_OPENMPI_BUILD) \
	  --build-arg 'arg_openmpi_pkg=$(KUBE_OPENMPI_PKG)' \
	  -t mpibase:latest -f Dockerfile.mpibase .

push_kubectl: build_kubectl
	docker tag kubectl-delivery:latest $(KUBECTL_IMAGE):$(KUBECTL_VERSION)
	docker push $(KUBECTL_IMAGE):$(KUBECTL_VERSION)
	docker tag kubectl-delivery:latest $(KUBECTL_IMAGE):latest
	docker push $(KUBECTL_IMAGE):latest

push_mpibase: build_mpibase
	docker tag mpibase:latest $(MPIBASE_IMAGE):$(MPIBASE_TAG)
	docker push $(MPIBASE_IMAGE):$(MPIBASE_TAG)
	docker tag mpibase:latest $(MPIBASE_IMAGE):latest
	docker push $(MPIBASE_IMAGE):latest

build: build_kubectl build_mpibase  ## build base images

push: build push_kubectl push_mpibase  ## push base images

metacontroller:  ## deploy metacontroller
	kubectl create namespace metacontroller
	kubectl apply -f https://raw.githubusercontent.com/GoogleCloudPlatform/metacontroller/master/manifests/metacontroller-rbac.yaml
	kubectl apply -f https://raw.githubusercontent.com/GoogleCloudPlatform/metacontroller/master/manifests/metacontroller.yaml

test:  ## test operator
	MPIBASE_IMAGE=$(MPIBASE_IMAGE) \
	REPLICAS=$(REPLICAS) \
	MYHOST=$(MYHOST) \
	 envsubst < mpi-test-replicas.yaml | kubectl apply -f - -n $(KUBE_NAMESPACE)

test-daemons:
	MPIBASE_IMAGE=$(MPIBASE_IMAGE) \
	REPLICAS=$(REPLICAS) \
	 envsubst < mpi-test-daemons.yaml | kubectl apply -f - -n $(KUBE_NAMESPACE)

logs: ## operator logs
	kubectl logs -l \
	app.kubernetes.io/instance=$(HELM_RELEASE),app.kubernetes.io/name=mpi-controller-mpi-operator \
	-n $(OPERATOR_NAMESPACE)

metalogs: ## show metacontroller POD logs
	@for i in `kubectl -n metacontroller get pods -l app.kubernetes.io/name=metacontroller -o=name`; \
	do \
	echo "---------------------------------------------------"; \
	echo "Logs for $${i}"; \
	echo kubectl -n metacontroller logs $${i}; \
	echo kubectl -n metacontroller get $${i} -o jsonpath="{.spec.initContainers[*].name}"; \
	echo "---------------------------------------------------"; \
	for j in `kubectl -n metacontroller get $${i} -o jsonpath="{.spec.initContainers[*].name}"`; do \
	RES=`kubectl -n metacontroller logs $${i} -c $${j} 2>/dev/null`; \
	echo "initContainer: $${j}"; echo "$${RES}"; \
	echo "---------------------------------------------------";\
	done; \
	echo "Main Pod logs for $${i}"; \
	echo "---------------------------------------------------"; \
	for j in `kubectl -n metacontroller get $${i} -o jsonpath="{.spec.containers[*].name}"`; do \
	RES=`kubectl -n metacontroller logs $${i} -c $${j} 2>/dev/null`; \
	echo "Container: $${j}"; echo "$${RES}"; \
	echo "---------------------------------------------------";\
	done; \
	echo "---------------------------------------------------"; \
	echo ""; echo ""; echo ""; \
	done

test-results:  ## show test results (logs)
	kubectl get pods -l job-name=mpioperator-test-mpi-launcher
	kubectl get pods -l job-name=mpioperator-test-mpi-launcher | \
	grep Completed | cut -f1 -d" " | xargs kubectl logs || true

test-clean:  ## clean down test
	kubectl delete -f mpi-test-replicas.yaml -n $(KUBE_NAMESPACE) || true
	sleep 1

regisry-creds: namespace
	@kubectl create secret -n $(KUBE_NAMESPACE) \
	  docker-registry $(PULL_SECRET) \
	 --docker-server=$(CI_REGISTRY) \
	 --docker-username=$(GITLAB_USER) \
	 --docker-password=$(REGISTRY_PASSWORD) \
	 --docker-email=$(GITLAB_USER_EMAIL) \
	-o yaml --dry-run | kubectl replace -n $(KUBE_NAMESPACE) --force -f -

clean: test-clean delete  ## Clean all

redeploy: clean deploy  ## redeploy operator

namespace: ## create the kubernetes namespace
	kubectl describe namespace $(KUBE_NAMESPACE) || kubectl create namespace $(KUBE_NAMESPACE)

delete_namespace: ## delete the kubernetes namespace
	@if [ "default" == "$(KUBE_NAMESPACE)" ] || [ "kube-system" == "$(KUBE_NAMESPACE)" ]; then \
	echo "You cannot delete Namespace: $(KUBE_NAMESPACE)"; \
	exit 1; \
	else \
	kubectl describe namespace $(KUBE_NAMESPACE) && kubectl delete namespace $(KUBE_NAMESPACE); \
	fi

deploy: namespace check  ## deploy the helm chart
	@helm template charts/$(HELM_CHART)/ --name $(HELM_RELEASE) \
				 --namespace $(OPERATOR_NAMESPACE) \
         --tiller-namespace $(OPERATOR_NAMESPACE) \
				 --set helmTests=false | kubectl -n $(OPERATOR_NAMESPACE) apply -f -

install: namespace  ## install the helm chart (with Tiller)
	@helm tiller run $(OPERATOR_NAMESPACE) -- helm install charts/$(HELM_CHART)/ --name $(HELM_RELEASE) \
		--wait \
		--namespace $(OPERATOR_NAMESPACE) \
		--tiller-namespace $(OPERATOR_NAMESPACE)

helm_delete: ## delete the helm chart release (with Tiller)
	@helm tiller run $(OPERATOR_NAMESPACE) -- helm delete $(HELM_RELEASE) --purge \
		--tiller-namespace $(OPERATOR_NAMESPACE)

show: ## show the helm chart
	@helm template charts/$(HELM_CHART)/ --name $(HELM_RELEASE) \
				 --namespace $(OPERATOR_NAMESPACE) \
         --tiller-namespace $(OPERATOR_NAMESPACE)

lint: ## lint check the helm chart
	@helm lint charts/$(HELM_CHART)/ \
				 --namespace $(OPERATOR_NAMESPACE) \
         --tiller-namespace $(OPERATOR_NAMESPACE)

delete: ## delete the helm chart release
	@helm template charts/$(HELM_CHART)/ --name $(HELM_RELEASE) \
				 --namespace $(OPERATOR_NAMESPACE) \
         --tiller-namespace $(OPERATOR_NAMESPACE) | kubectl -n $(OPERATOR_NAMESPACE) delete -f -

describe: ## describe Pods executed from Helm chart
	@for i in `kubectl -n $(OPERATOR_NAMESPACE) get pods -l app.kubernetes.io/instance=$(HELM_RELEASE) -o=name`; \
	do echo "---------------------------------------------------"; \
	echo "Describe for $${i}"; \
	echo kubectl -n $(OPERATOR_NAMESPACE) describe $${i}; \
	echo "---------------------------------------------------"; \
	kubectl -n $(OPERATOR_NAMESPACE) describe $${i}; \
	echo "---------------------------------------------------"; \
	echo ""; echo ""; echo ""; \
	done

helm_tests:  ## run Helm chart tests
	helm tiller run $(OPERATOR_NAMESPACE) -- helm test $(HELM_RELEASE) --cleanup

helm_dependencies: ## Utility target to install Helm dependencies
	@which helm ; rc=$$?; \
	if [ $$rc != 0 ]; then \
	curl "https://kubernetes-helm.storage.googleapis.com/helm-$(HELM_VERSION)-linux-amd64.tar.gz" | tar zx; \
	mv linux-amd64/helm /usr/bin/; \
	helm init --client-only; \
	fi
	@helm init --client-only
	@if [ ! -d $$HOME/.helm/plugins/helm-tiller ]; then \
	echo "installing tiller plugin..."; \
	helm plugin install https://github.com/rimusz/helm-tiller; \
	fi
	helm version --client
	@helm tiller stop 2>/dev/null || true

kubectl_dependencies: ## Utility target to install K8s dependencies
	@([ -n "$(KUBE_CONFIG_BASE64)" ] && [ -n "$(KUBECONFIG)" ]) || (echo "unset variables [KUBE_CONFIG_BASE64/KUBECONFIG] - abort!"; exit 1)
	@which kubectl ; rc=$$?; \
	if [[ $$rc != 0 ]]; then \
		curl -L -o /usr/bin/kubectl "https://storage.googleapis.com/kubernetes-release/release/$(KUBERNETES_VERSION)/bin/linux/amd64/kubectl"; \
		chmod +x /usr/bin/kubectl; \
		mkdir -p /etc/deploy; \
		echo $(KUBE_CONFIG_BASE64) | base64 -d > $(KUBECONFIG); \
	fi
	@echo -e "\nkubectl client version:"
	@kubectl version --client
	@echo -e "\nkubectl config view:"
	@kubectl config view
	@echo -e "\nkubectl config get-contexts:"
	@kubectl config get-contexts
	@echo -e "\nkubectl version:"
	@kubectl version

kubeconfig: ## export current KUBECONFIG as base64 ready for KUBE_CONFIG_BASE64
	@KUBE_CONFIG_BASE64=`kubectl config view --flatten | base64 -w 0`; \
	echo "KUBE_CONFIG_BASE64: $$(echo $${KUBE_CONFIG_BASE64} | cut -c 1-40)..."; \
	echo "appended to: PrivateRules.mak"; \
	echo -e "\n\n# base64 encoded from: kubectl config view --flatten\nKUBE_CONFIG_BASE64 = $${KUBE_CONFIG_BASE64}" >> PrivateRules.mak

help:  ## show this help.
	@echo "make targets:"
	@grep -E '^[0-9a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | sort | awk 'BEGIN {FS = ": .*?## "}; {printf "\033[36m%-30s\033[0m %s\n", $$1, $$2}'
	@echo ""; echo "make vars (+defaults):"
	@grep -E '^[0-9a-zA-Z_-]+ \?=.*$$' $(MAKEFILE_LIST) | sort | awk 'BEGIN {FS = " \\?\\= "}; {printf "\033[36m%-30s\033[0m %s\n", $$1, $$2}'
