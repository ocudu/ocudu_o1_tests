<!--
SPDX-FileCopyrightText: Copyright (C) 2021-2026 Software Radio Systems Limited
SPDX-License-Identifier: BSD-3-Clause-Open-MPI
-->

OCUDU O1 Integration Tests
===

# Checkout git submodules

This repository contains two git submodules which have to be initiated locally. They are in `/ocudu_elements` and point to the repos `ocudu_o1_adapter` and `ocudu_netconf` sitting in [OCUDU ORAN applications](https://gitlab.com/ocudu/ocudu_elements/ocudu_oran_apps).

To initiate them initially after cloning this repo, call `git submodule update --init --recursive`.

Thereafter, we need to explicitly keep the submodules up-to-date, even after running a local `git update` on this repo.
To do so, call `git submodule update --recursive`.

Usually they should point to the head of the two projects, but if you need them to point to another commit simply `cd` to the repo you want to change (e.g. `cd ocudu_elements/ocudu_o1_adapter`) and call `git fetch` followed by `git checkout <insert checkout hash>`. 

# Running self-contained integration tests manually

This launches the tests but keeps the containers running even after the test cases completed.

```bash
$ docker compose --profile test up --build
```

# Launch self-contained integration tests for CI purposes

```bash
$ ./run_tests.py
```


# Launch OCUDU O1 containers standalone

```bash
docker compose -f docker-compose.yml --profile dev up
```

The netconf server will listen on `172.21.0.14:830` by default.


# Launch minimal SMO standalone 

```bash
docker compose -f docker-compose.smo.yml --env-file smo.env up
```

By default the SMO web interface is available under http://172.21.0.100:8080. Login is `admin/admin`.
Note that it might take a while for the services to become ready and to login.


# Launch all together
```bash
docker compose -f docker-compose.smo.yml --env-file smo.env -f docker-compose.yml --profile dev up
```

When launching both docker yml's you can connect to the SMO web interface as described in the section [above](#launch-minimal-smo-standalone).
Then, to connect to the netconf server of the gNB, go to "Connect->Add Node" and add a new connection using the local IP, i.e. `172.21.0.14`. Credentials will be `root/root` by default.