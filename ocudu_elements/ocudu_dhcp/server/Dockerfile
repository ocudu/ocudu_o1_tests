# SPDX-FileCopyrightText: Copyright (C) 2021-2026 Software Radio Systems Limited
# SPDX-License-Identifier: BSD-3-Clause-Open-MPI

FROM alpine:3.22

# Add Kea packages from cloudsmith. Make sure the version matches that of the Alpine version.
# Also, install all the open source hooks. When updating, new instructions can
# be found at: https://cloudsmith.io/~isc/repos/kea-2-5/setup/#formats-alpine
ARG VERSION=3.0

SHELL ["/bin/ash", "-o", "pipefail", "-c"]
RUN cp /etc/apk/repositories /etc/apk/repositories_backup && \
    # Install curl and bash for cloudsmith's script. Tzdata to allow timezone change.
    apk update && apk add --no-cache curl bash tzdata && \
    # Setup Cloudsmith repo
    if test "$(expr "$(echo "${VERSION}" | cut -d '.' -f 2)" % 2)" = 0; then \
        repo="kea-$(echo "${VERSION}" | cut -d '.' -f 1)-$(echo "${VERSION}" | cut -d '.' -f 2)"; \
    else \
        repo='kea-dev'; \
    fi && \
    curl -1sLf "https://dl.cloudsmith.io/public/isc/${repo}/setup.alpine.sh" | bash && \
    apk update && \
    # Install open-source Kea packages.
    apk add --no-cache \
        isc-kea-dhcp4~=${VERSION} \
        isc-kea-mysql~=${VERSION} \
        isc-kea-pgsql~=${VERSION} \
        isc-kea-hooks~=${VERSION} && \
    # Revert Cloudsmith repositories
    mv /etc/apk/repositories_backup /etc/apk/repositories && \
    # remove curl and bash
    apk del curl bash

COPY entrypoint.sh /usr/local/bin
COPY generate_option43_hex.sh /usr/local/bin
COPY dhcp_server.env /etc/kea/dhcp_server.env

ENTRYPOINT [ "/usr/local/bin/entrypoint.sh" ]