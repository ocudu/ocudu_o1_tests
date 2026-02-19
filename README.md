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

# Launch minimal SMO standalone 

```bash
docker compose -f docker-compose.smo.yml --env-file smo.env up
```

By default the SMO web interface is available under http://172.21.0.100:8080. Login is `admin/admin`.