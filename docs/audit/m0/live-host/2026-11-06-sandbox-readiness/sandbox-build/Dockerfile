# SR-06 disposable sandbox image (container fallback). Build-time network only; no credentials.
FROM node:22-bookworm-slim@sha256:5ec23b7c622b00547753bc6ffd78085dd868a68aeb41c8e0a1418f033e13d96d

ARG CLAUDE_CODE_VERSION=2.1.318
ARG V1_HOOKS_SHA256=bbbdcf1f6b1dd4f525eded64f7ca77adf34afbd50ced76514aa0b4a86c2040d1

RUN apt-get update && apt-get install -y --no-install-recommends \
      bash git jq curl ca-certificates procps iproute2 python3 python3-venv python3-pip build-essential python3-dev \
    && rm -rf /var/lib/apt/lists/*

# Pinned Claude Code; npm verifies the registry integrity hash of the tarball.
RUN npm install -g "@anthropic-ai/claude-code@${CLAUDE_CODE_VERSION}" \
    && claude --version | tee /opt/claude-version.txt \
    && npm ls -g --depth=0 > /opt/npm-global.txt

# Non-root operator account; no sudo installed.
RUN useradd --create-home --uid 1500 --shell /bin/bash tester

# V1 baseline (git archive of ebdafe5), checksum-verified.
COPY writ-v1-ebdafe5.tar /opt/writ-v1-ebdafe5.tar
RUN mkdir -p /opt/writ-v1 && tar -xf /opt/writ-v1-ebdafe5.tar -C /opt/writ-v1 \
    && echo "${V1_HOOKS_SHA256}  /opt/writ-v1/hooks/hooks.json" | sha256sum -c - \
    && tar -tf /opt/writ-v1-ebdafe5.tar | grep -v '/$' | sort > /opt/writ-v1-files.txt

# Arm C2 copy: onFailure added to the 14 PreToolUse registrations only.
COPY make_c2.py /opt/sr06/make_c2.py
RUN cp -a /opt/writ-v1 /opt/writ-v1-onfailure \
    && python3 /opt/sr06/make_c2.py /opt/writ-v1-onfailure/hooks/hooks.json \
    && cd /opt && while read -r f; do \
         if [ "$f" != "hooks/hooks.json" ]; then cmp -s "writ-v1/$f" "writ-v1-onfailure/$f" || { echo "DIFF $f"; exit 1; }; fi; \
       done < /opt/writ-v1-files.txt \
    && echo "C2 byte-identity: all files except hooks/hooks.json identical" > /opt/c2-identity.txt \
    && diff /opt/writ-v1/hooks/hooks.json /opt/writ-v1-onfailure/hooks/hooks.json > /opt/c2-hooks.diff || true

# Per-arm venvs: V1 requires its venv to import writ from its own plugin root.
# pyyaml and tokenizers are imported by V1 at runtime but not declared in its pyproject.
RUN for root in /opt/writ-v1 /opt/writ-v1-onfailure; do \
      python3 -m venv "$root/.venv" && "$root/.venv/bin/pip" install --quiet --upgrade pip \
      && "$root/.venv/bin/pip" install --quiet -e "$root" \
      && "$root/.venv/bin/pip" install --quiet pyyaml tokenizers \
      && "$root/.venv/bin/pip" freeze > "/opt/$(basename $root)-freeze.txt"; \
    done

# One-off ONNX export (build-time tooling in a throwaway venv, removed afterwards).
USER tester
RUN python3 -m venv /tmp/onnx-venv \
    && /tmp/onnx-venv/bin/pip install --quiet --upgrade pip \
    && /tmp/onnx-venv/bin/pip install --quiet torch --index-url https://download.pytorch.org/whl/cpu \
    && /tmp/onnx-venv/bin/pip install --quiet "optimum[onnxruntime]>=2.0,<3" transformers \
    && cd /opt/writ-v1 && /tmp/onnx-venv/bin/python scripts/export_onnx.py \
    && rm -rf /tmp/onnx-venv /home/tester/.cache/huggingface /home/tester/.cache/pip
USER root

# V1 trees read-only after venv creation (arm A must be unmodifiable).
RUN chmod -R a-w /opt/writ-v1 /opt/writ-v1-onfailure

COPY probes/ /opt/probes/
COPY services/ /opt/sr06/services/
COPY init-ws.sh /opt/sr06/init-ws.sh
RUN chmod 0555 /opt/probes/*.sh /opt/probes/*.py /opt/sr06/services/*.py /opt/sr06/init-ws.sh

USER tester
WORKDIR /home/tester
RUN /opt/sr06/init-ws.sh
ENV SR06_RUN=/home/tester/run/current \
    XDG_STATE_HOME=/home/tester/.local/state \
    DISABLE_AUTOUPDATER=1 \
    CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1
CMD ["sleep", "infinity"]
