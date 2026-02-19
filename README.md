OCUDU O1 Integration Tests
===

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

To connect to the netconf server of the gNB, go to "Connect->Add Node" and add a new
connection using the local IP, i.e. `172.21.0.14`. Credentials will be `root/root` by default.



# Launch all together
```bash
docker compose -f docker-compose.smo.yml -f docker-compose.yml --profile dev up
```
