# Baut zeit.exe (Windows x64) mit Windows-Python und PyInstaller unter Wine.
# Aufruf: packaging/build.sh windows-x64
FROM debian:trixie-slim

ARG PYTHON_VERSION=3.12.10

RUN dpkg --add-architecture i386 \
 && apt-get update \
 && apt-get install -y --no-install-recommends wine wine64 ca-certificates curl unzip \
 && rm -rf /var/lib/apt/lists/*

ENV WINEPREFIX=/wine WINEDEBUG=-all WINEARCH=win64 PYTHONUTF8=1
RUN wineboot --init && wineserver -w

# Das NuGet-Paket enthält ein vollständiges Python samt pip, ohne Installer.
RUN curl -fsSL -o /tmp/python.zip "https://www.nuget.org/api/v2/package/python/${PYTHON_VERSION}" \
 && unzip -q /tmp/python.zip 'tools/*' -d /tmp/py \
 && mv /tmp/py/tools "$WINEPREFIX/drive_c/Python" \
 && rm -rf /tmp/python.zip /tmp/py

RUN wine 'C:\Python\python.exe' -m pip install --no-cache-dir --disable-pip-version-check \
      pyinstaller requests truststore requests-negotiate-sspi \
 && wineserver -w

WORKDIR /src
# Argumente gehen an PyInstaller; wineserver -w wartet, bis Wine alle Dateien geschrieben hat.
ENTRYPOINT ["sh", "-c", "wine 'C:\\Python\\python.exe' -m PyInstaller --noconfirm --clean --onefile --name zeit --workpath /tmp/build --specpath /tmp \"$@\" && wineserver -w", "--"]
