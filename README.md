<!--
SPDX-FileCopyrightText: Copyright (C) 2021-2026 Software Radio Systems Limited
SPDX-License-Identifier: BSD-3-Clause-Open-MPI
-->

OCUDU O1 Integration Tests
===

[![Pipeline](https://gitlab.com/ocudu/ocudu_elements/ocudu_oran_apps/ocudu_o1_tests/badges/main/pipeline.svg)](https://gitlab.com/ocudu/ocudu_elements/ocudu_oran_apps/ocudu_o1_tests/-/pipelines?scope=branches)
[![License](https://img.shields.io/badge/license-BSD--3--Clause--Open--MPI-blue)](https://spdx.org/licenses/BSD-3-Clause-Open-MPI.html)

# Checkout git submodules

This repository contains two git submodules which have to be initiated locally. They are in `/ocudu_elements` and point to the repos `ocudu_o1_adapter` and `ocudu_netconf` sitting in [OCUDU ORAN applications](https://gitlab.com/ocudu/ocudu_elements/ocudu_oran_apps).

To initiate them initially after cloning this repo, call `git submodule update --init --recursive`.

Thereafter, we need to explicitly keep the submodules up-to-date, even after running a local `git update` on this repo.
To do so, call `git submodule update --recursive`.

Usually they should point to the head of the two projects, but if you need them to point to another commit simply `cd` to the repo you want to change (e.g. `cd ocudu_elements/ocudu_o1_adapter`) and call `git fetch` followed by `git checkout <insert checkout hash>`. 

# Running self-contained integration tests manually

This launches the tests but keeps the containers running even after the test cases completed.

```bash
$ NETCONF_ARGS="--config gnb --enable-tls" docker compose --profile test up --build
```

Setting `NETCONF_ARGS="--config gnb --enable-tls"` turns on the TLS endpoint so the `test_netconf_over_tls_rfc_7589` case has a port to connect to. Without it, netconf starts SSH-only on `:830` and the TLS test times out.

This manual path only covers the `gnb` profile against the bundled netconf config. The other profiles (`cu`, `cucp`, `cuup`, `du`) and the per-profile custom XMLs under `tests/configs/<profile>/` require `run_tests.py` (see below), which sets `O1_ADAPTER_PROFILE`, `NETCONF_ARGS`, and `PYTEST_ADDOPTS` per iteration.

# Launch automated self-contained integration tests

`run_tests.py` takes a netconf profile and runs the matching `tests/test_o1_adapter_<profile>.py` suite. Valid profiles are `gnb`, `cu`, `cucp`, `cuup`, and `du`.

```bash
$ ./run_tests.py <profile>
```

For each profile the suite runs once with the bundled config baked into the netconf image, then once per custom XML under `tests/configs/<profile>/`.

By default `run_tests.py` reuses existing images. When iterating on the `ocudu_netconf` or `ocudu_o1_adapter` submodules locally, pass `--build` so the images are rebuilt from your checkouts first — otherwise stale images are reused silently (`o1_adapter` is pinned to `:latest`, and uncommitted netconf changes don't bump its SHA tag):

```bash
$ ./run_tests.py --build <profile>
```

CI omits `--build` and pulls the prebuilt netconf image by submodule SHA instead.

To inspect the JUnit XML reports after the run, set `O1_TEST_RESULTS_DIR` to a host path before invoking the script. Without it, results are written to a docker volume that gets wiped by the `down --volumes` between iterations.

```bash
$ mkdir -p log
$ O1_TEST_RESULTS_DIR=$PWD/log ./run_tests.py <profile>
```

After the run, `log/` contains one `out_<profile>_<label>.xml` per iteration plus a merged `out.xml`.

`run_tests.py` starts the netconf container with `--enable-tls`, exposing a NETCONF-over-TLS endpoint on port `6513` alongside the SSH endpoint on `830`. The certs land in a shared `netconf-tls-certs` named volume, which the test container mounts read-only and reads from to connect via mTLS. The volume is wiped between iterations by `docker compose down -v`.

See [`ocudu_elements/ocudu_netconf/README.md`](ocudu_elements/ocudu_netconf/README.md) for the netconf container's `--enable-tls` flag, the dual-mode cert dir behavior, and the `cert-to-name` mapping that turns a client cert's CN into the NETCONF username.


# Launch OCUDU O1 containers standalone

```bash
docker compose -f docker-compose.yml --profile dev up
```

The netconf server will listen on `172.21.0.14:830` by default.

To bring the stack up with TLS enabled, pass `--enable-tls` in `NETCONF_ARGS`:

```bash
NETCONF_ARGS="--config gnb --enable-tls" docker compose -f docker-compose.yml --profile dev up
```

Both endpoints come up: SSH on `830`, TLS on `6513`. The netconf container provisions certs into the shared `netconf-tls-certs` named volume. See [`ocudu_elements/ocudu_netconf/README.md`](ocudu_elements/ocudu_netconf/README.md) for the self-sign vs. operator-provisioned modes and how to extract the client cert/key for an external NETCONF client.


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

# Development / CI setup
The CI setup of this repository can be summarized in the following:
- Merge requests will be merged into `dev`
- Submodule pointers will be automatically updated in `dev`
- Changes in `dev` will be pushed to `main` on a successful nightly pipeline run
- Submodule pointers in `main` will always lead to a working reference combination of this repo, [OCUDU O1 Adapter](https://gitlab.com/ocudu/ocudu_elements/ocudu_oran_apps/ocudu_o1_adapter), and [OCUDU Netconf service](https://gitlab.com/ocudu/ocudu_elements/ocudu_oran_apps/ocudu_netconf)

## License

This project is licensed under the BSD 3-Clause Open MPI variant License – see the [LICENSE](./LICENSE) file for details.
Portions of this software may implement 3GPP specifications, which may be subject to additional licensing requirements.