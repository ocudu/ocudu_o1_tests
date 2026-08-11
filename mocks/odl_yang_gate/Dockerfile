# SPDX-FileCopyrightText: Copyright (C) 2021-2026 Software Radio Systems Limited
# SPDX-License-Identifier: BSD-3-Clause-Open-MPI

# YANGTOOLS_VERSION must track the YangTools shipped by the SDNC image in
# smo.env (SDNC_IMAGE), which reports it under
# /opt/opendaylight/system/org/opendaylight/yangtools/yang-parser-impl/.
# 3.1.1-STAGING-latest carries 11.0.5. Note that tag floats, so the SMO can move
# under us with no diff here to review -- the pin cannot drift visibly, and a
# stale pin silently voids the "faithful oracle" property this gate rests on. A
# nightly job asserting the two still agree is the only thing that would catch
# it; pulling the 1.5 GB ONAP image on the MR path would wreck the cheap-gate
# property instead.
ARG YANGTOOLS_VERSION=11.0.5

# Coursier's linux launcher is a native binary, so resolving the jars needs no
# JVM -- a slim base here saves ~180 MB of per-build download over a JDK image.
FROM debian:bookworm-slim AS jars
ARG YANGTOOLS_VERSION
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*
RUN curl -sfL \
      https://github.com/coursier/coursier/releases/download/v2.1.24/cs-x86_64-pc-linux.gz \
      -o /tmp/cs.gz \
    && gunzip /tmp/cs.gz \
    && chmod +x /tmp/cs
# Stage every jar under one directory so the runtime stage can use a wildcard
# classpath instead of coursier's cache layout.
RUN mkdir -p /opt/yangtools/lib \
    && /tmp/cs fetch --classpath \
         "org.opendaylight.yangtools:yang-model-validator:${YANGTOOLS_VERSION}" \
       | tr ':' '\n' \
       | xargs -I{} cp {} /opt/yangtools/lib/

FROM eclipse-temurin:21-jre-noble

ENV PIP_NO_CACHE_DIR=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends python3 python3-pip \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /opt/odl_yang_gate

COPY requirements.txt .
RUN pip3 install --break-system-packages -r requirements.txt

COPY --from=jars /opt/yangtools/lib /opt/yangtools/lib

ENV YANGTOOLS_CLASSPATH="/opt/yangtools/lib/*"

# Baked in so the image runs standalone, but docker-compose.yml mounts both over
# the top: `docker compose run` reuses an existing :latest without re-checking
# the build context, and a stale image enforcing an old baseline while reporting
# green is the one result this gate must never produce.
COPY odl_yang_gate.py known_failures.yaml ./

ENTRYPOINT ["python3", "/opt/odl_yang_gate/odl_yang_gate.py"]
