# Minimal ONAP-based SMO


## Prepare network

Run `docker network ls` and if `smo` network doesn't exist, create custom Docker network with specific gateway and subnet:

`$ docker network create --gateway 172.21.0.1 --subnet 172.21.0.0/24 smo`


## Run

`$ docker compose up`

## Access

The ONAP SMO should be available under http://localhost:8080 with login `admin/admin`.


http://localhost:8080/apidoc/explorer/index.html