FROM python:3.13-slim

# git is needed to install xnat-api-client from its repository
RUN apt-get update && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir pydicom==3.0.2 \
        git+https://github.com/Western-Australian-NIF-Node/xnat-api-client.git

WORKDIR /app
COPY register_rt_collections.py /app/

# The XNAT Container Service supplies the command line from command.json,
# so no ENTRYPOINT/CMD is needed. This default makes `docker run` self-describing.
CMD ["python", "/app/register_rt_collections.py", "--help"]
