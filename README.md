# MPI Operator

Developed with [MetaController](https://metacontroller.app/) and based on https://github.com/everpeace/kube-openmpi and https://github.com/kubeflow/mpi-operator.

This MPI Kubernetes [Operator](https://coreos.com/operators/) provides a Kubernetes native interface to building MPI clusters and running jobs.

## Deploy

First you must have MetaController:
```shell
make metacontroller
```

Next deploy the Operator:
```shell
make deploy
```

## Test

An MPI cluster relies on a base image that encapsulates the MPI application dependencies and facilitates the MPI communication.  An example of this is the included ```mpibase``` image, which can be built using: 
```shell
make build_mpibase && make push_mpibase
```
Ensure that you configure your own Docker registry details by setting appropriate values for:
```
PULL_SECRET = "gitlab-registry"
GITLAB_USER = you
REGISTRY_PASSWORD = your-registry-password
GITLAB_USER_EMAIL = "you@somewhere.net"
CI_REGISTRY = gitlab.somewhere.com
CI_REPOSITORY = repository/uri
MPIBASE_IMAGE = $(CI_REGISTRY)/$(CI_REPOSITORY)/mpibase:latest
```
set in PrivateRules.mak


Launch the helloworld job:
```shell
make test
```

Once everything starts, the logs are available in the `launcher` pod.
