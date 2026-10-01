# Baut zeit für Linux (x64 oder arm64, je nach --platform) mit PyInstaller.
# Ubuntu 20.04 hat glibc 2.31, das Binary läuft also ab Ubuntu 20.04, Debian 11 und RHEL 9.
# AlmaLinux 8 (glibc 2.28) ginge weiter zurück, dort hängt dnf aber unter Rosetta-Emulation.
# Aufruf: packaging/build.sh linux-x64 linux-arm64
FROM ubuntu:20.04

ARG PYTHON_VERSION=3.12

# gssapi gibt es für Linux nur als Quellpaket; zum Bauen braucht es Compiler und MIT-Kerberos-Header.
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update \
 && apt-get install -y --no-install-recommends gcc libc6-dev libkrb5-dev ca-certificates \
 && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv
ENV UV_PYTHON_INSTALL_DIR=/opt/uv-python
RUN uv venv /venv --python "$PYTHON_VERSION" \
 && uv pip install --python /venv --no-cache pyinstaller requests truststore requests-gssapi playwright

WORKDIR /src
ENTRYPOINT ["/venv/bin/pyinstaller", "--noconfirm", "--clean", "--onefile", "--name", "zeit", \
            "--collect-submodules", "gssapi", "--workpath", "/tmp/build", "--specpath", "/tmp"]
