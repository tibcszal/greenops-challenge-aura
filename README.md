# GreenOps Challenge Solution

We have built a program that chooses when and where cloud workloads run, and it reduces their electricity emissions. Code runs on our computer, controls EXOSCALE simulator through provided APIs.

Our first simulation consumed ~36 kg CO2e and we could, in the end, get it down to 21.9 kg CO2e. This repo contains the source code for the orchestrator.

In order to run the code, follow these steps:

## 1. Download credentials

Sign in at [greenops.containerstack.net](https://greenops.containerstack.net),
create or join a team, then click **New simulation run**.

Open that run’s **API credentials → Other credentials and advanced setup → Download environment file** to download
`greenops-<run-id>.env`.

## 2. Obtain the code

You need Git and Docker installed on your computer.

```sh
git clone https://github.com/tibcszal/greenops-challenge-aura.git
cd greenops-challenge-aura
```

Move the downloaded .env file into this folder and rename it to
`greenops.env`. It is ignored by Git.

## 3. Setup your environment

```sh
python3 -m venv .venv && .venv/bin/pip install --require-hashes -r requirements.txt
make env
```

## 4. Test (optional)

```sh
PATH="$PWD/.venv/bin:$PATH" make test
```

Preview what the orchestrator would place, offline and without credentials:

```sh
.venv/bin/python -m orchestrator.run plan
```

## 5. Run the orchestrator

Credentials are read from `greenops.env` in this folder (`make env` shows which
file is picked). Pick one way to run, locally or in Docker. Keep that terminal
open and watch your run's dashboard on the website.

Run only one orchestrator per run. Never start the local and the Docker version
against the same run at the same time.

### Locally

```sh
make run
```

Useful variations:

```sh
make run ARGS="--margin 1.6"      # higher margin = pauses less
```

### In Docker

Docker commands need `sudo` on some machines; the Makefile uses `sudo docker`
by default (override with `make DOCKER=docker ...`).

```sh
make docker-build                 # builds greenops-orchestrator:local
make docker-run                   # live run, mounts greenops.env and .local/state
make docker-run ARGS="--margin 1.6"
```

The same without `make`:

```sh
mkdir -p .local/state
sudo docker run --rm -it \
  -v "$PWD/greenops.env:/opt/greenops-starter/greenops.env:ro" \
  -v "$PWD/.local/state:/state" -e GREENOPS_STATE=/state/orch.json \
  greenops-orchestrator:local
```

Credentials are never baked into the image; they are mounted read-only at run time.

## 6. Clean up after a crash

If a run stops unexpectedly, delete the leftover VMs:

```sh
make cleanup                      # local
make docker-cleanup               # Docker
```

[Verification](docs/VERIFICATION.md) · [Third-party software](docs/THIRD_PARTY.md)
