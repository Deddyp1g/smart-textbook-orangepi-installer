FROM smart-textbook/orangepi-cpu-base:jammy-22.04.5-arm64

RUN apt-get update -o Acquire::Retries=2 \
    && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
        bash ca-certificates build-essential cmake file libgomp1 libopenblas0-pthread \
        libnuma1 libssl3 pkg-config python3 python3-dev python3-matplotlib python3-venv \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd -g 20000 student \
    && useradd -u 20000 -g 20000 -d /home/student -s /bin/bash student \
    && install -d -o student -g student -m 0700 /home/student

COPY --chmod=0644 ascend.bashrc /etc/smart-textbook-ascend.bashrc
RUN install -d -m 0755 /lib64 \
    && ln -s /lib/ld-linux-aarch64.so.1 /lib64/ld-linux-aarch64.so.1
USER 20000:20000
WORKDIR /home/student
ENV HOME=/home/student USER=student LOGNAME=student PYTHONDONTWRITEBYTECODE=1
CMD ["/bin/sleep", "infinity"]
