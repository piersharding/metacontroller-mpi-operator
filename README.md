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

An MPI cluster relies on a base image that encapsulates the MPI application dependencies and facilitates the MPI communication.  An example of this is the included `mpibase` image, which can be built using: 
```shell
make build_mpibase && make push_mpibase
```
You can use the default images on [Docker Hub](https://hub.docker.com/r/piersharding/) or you must ensure that you configure your own Docker registry details by setting appropriate values for:
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

## Scheduling modes

The CRD for MPIJobs has two parameters: `replicas(int)` and `daemons(boolean)`.  Specifying only `replicas` will leave it up to the scheduler where to place the worker pods on the cluster, but if in addition `daemons` is set to `true` (see [mpi-test-demons.yaml](https://github.com/piersharding/metacontroller-mpi-operator/blob/master/mpi-test-daemons.yaml)) then the Pod AntiAffinity rules are applied and the Kubernetes scheduler will force the workers onto individual nodes - if available.
initContainers check availability of the workers, prior to executing the `launcher`, so if any Pods are stuck in `Pending` then they are dropped out of the worker list.