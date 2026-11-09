# syntax=docker/dockerfile:1
# argus-mint serve: the card workflow over HTTP (drafts, reviews, publishing) for
# argus-studio and argus-assay. Configure with ARGUS_MINT_* (see README "Running the service").
FROM python:3.12-slim

# Pin past the eMudhra/emSign incident (discuss.python.org/t/109576). A floating
# :latest tag can stay cached on a pre-0.13.4 digest.
COPY --from=ghcr.io/astral-sh/uv:0.13.5 /uv /uvx /bin/

WORKDIR /app

# Distrust emSign ECC Root CA - G3 and the Comodo AAA cross-sign root, then
# replace the base image's pip. pip < 26.2.2 trusts its vendored bundle in
# addition to the system store.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates \
    && sed -i \
        -e 's|^mozilla/emSign_ECC_Root_CA_-_G3|!mozilla/emSign_ECC_Root_CA_-_G3|' \
        -e 's|^mozilla/Comodo_AAA|!mozilla/Comodo_AAA|' \
        /etc/ca-certificates.conf \
    && update-ca-certificates --fresh >/dev/null \
    && rm -rf /var/lib/apt/lists/* \
    && python -m pip install --no-cache-dir \
        "pip @ https://files.pythonhosted.org/packages/3f/06/3e4420302363fbb71be3c7f35ce042f54bfca330e3e66b3a2e3c4cca579f/pip-26.2.2-py3-none-any.whl#sha256=fd7f4dfa8190e014013b9f08f3e973f4e4c26ffd179a204895533a7254813a70"

# hatch-vcs would read the version from git; the image has no git and no .git, so the
# release job passes the tag's version in (a local build is 0.0.0+docker).
ARG VERSION=0.0.0+docker
ENV SETUPTOOLS_SCM_PRETEND_VERSION=${VERSION}
COPY . .

RUN uv pip install --system --no-cache "certifi>=2026.10.19" ".[server]"

# The example templates, frames and bios ship in /app; mount your own over them.
# Drafts (and their published vouchers) live on a volume.
ENV ARGUS_MINT_TEMPLATES_DIR=/app/templates \
    ARGUS_MINT_FRAMES_DIR=/app/frames \
    ARGUS_MINT_BIOS_DIR=/app/bios \
    ARGUS_MINT_DRAFTS_DIR=/data/drafts
VOLUME /data
EXPOSE 8108
CMD ["argus-mint", "serve", "--host", "0.0.0.0", "--port", "8108"]
