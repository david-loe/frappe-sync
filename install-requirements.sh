#!/usr/bin/env bash
set -e

export DEBIAN_FRONTEND=noninteractive

. /etc/os-release
MS_VERSION="$VERSION_ID"
[ "$ID" = "debian" ] && MS_VERSION="${VERSION_ID%%.*}"

apt-get update
apt-get install --no-install-recommends -y ca-certificates curl

curl -fsSL -o /tmp/packages-microsoft-prod.deb \
  "https://packages.microsoft.com/config/$ID/$MS_VERSION/packages-microsoft-prod.deb"
dpkg -i /tmp/packages-microsoft-prod.deb
rm /tmp/packages-microsoft-prod.deb

apt-get update
ACCEPT_EULA=Y apt-get install --no-install-recommends -y \
  msodbcsql18 \
  unixodbc-dev \
  libfbclient2 \

rm -rf /var/lib/apt/lists/*
