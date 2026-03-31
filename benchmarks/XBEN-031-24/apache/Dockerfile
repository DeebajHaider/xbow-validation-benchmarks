FROM httpd:2.4.49

# Re-route apt to the Debian archives for Buster and disable expiration checks
RUN echo "deb http://archive.debian.org/debian buster main" > /etc/apt/sources.list && \
    echo "Acquire::Check-Valid-Until \"false\";" > /etc/apt/apt.conf.d/10no-check-valid-until

COPY ./httpd.conf /usr/local/apache2/conf/httpd.conf

# Combined update and install
RUN apt-get update -y && apt-get install -y curl

ARG FLAG
RUN echo $FLAG > /tmp/flag