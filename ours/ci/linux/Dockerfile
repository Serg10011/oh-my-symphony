FROM node:24-bookworm-slim@sha256:01d566e8d94d8c619591d78652babfccd3c6992a3dffa9ed5ab39e11cf6c70e9 AS node
FROM python:3.14-slim-bookworm@sha256:e6f981aeb339e6ead5e5e87e14c55b715ac76d6b2a710b86d70ca69c7f2399ce

RUN apt-get update && apt-get install -y --no-install-recommends \
    bash ca-certificates curl git libatomic1 openssh-client procps rsync unzip \
    && rm -rf /var/lib/apt/lists/*
COPY --from=node /usr/local/bin/node /usr/local/bin/node
COPY --from=node /usr/local/lib/node_modules/npm /usr/local/lib/node_modules/npm
RUN ln -s ../lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm \
    && ln -s ../lib/node_modules/npm/bin/npx-cli.js /usr/local/bin/npx
COPY requirements.txt /opt/check/requirements.txt
RUN python -m pip install --disable-pip-version-check --no-cache-dir --no-deps \
    --only-binary=:all: --require-hashes -r /opt/check/requirements.txt \
    && python -m pip check && node --version && npm --version
COPY probe.py suite.sh summarize.py /opt/check/
RUN useradd --uid 10001 --no-create-home --shell /bin/bash tester
USER 10001:10001
ENV HOME=/tmp/home PYTHONDONTWRITEBYTECODE=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /tmp
