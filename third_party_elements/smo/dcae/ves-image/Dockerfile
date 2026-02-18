ARG BASEIMAGE=nexus3.onap.org:10001/onap/org.onap.dcaegen2.collectors.ves.vescollector:1.8.0

FROM ubuntu:22.04 AS stage-0
ARG DLREPO=https://forge.3gpp.org/rep/sa5/MnS.git
ARG DLBRANCH=Tag_Rel16_SA104
RUN apt update && apt install -y python3  git

COPY install.py /

RUN python3 install.py $DLREPO $DLBRANCH /opt/app/VESCollector/etc/externalRepo


FROM ${BASEIMAGE}


COPY --from=stage-0 --chown=vescollector:vescollector /opt/app/VESCollector/etc/externalRepo /opt/app/VESCollector/etc/externalRepo
