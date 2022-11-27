# Mumble soundboard (MSB) v2
# go version
#
# This image makes use of the following software:
#
# 1. gomumblesoundboard base by: https://github.com/robbi5/gomumblesoundboard
#		The MIT License (MIT)
# 2. Webserver integration inspired by: https://github.com/gigablah/alpine-php
#		The MIT License (MIT)
# 3. File manager: ELfinder - http://elfinder.org/#elf_l1_Lw
# 		Copyright (c) 2009-2016, Studio 42
#		All rights reserved.
# 4. Additional web code by #freaks on EFnet
#
# Please note the comments in this Docker file for running a container properly
#
#

FROM gymnae/alpine-base:latest

MAINTAINER      Gunnar Falk <docker@grundstil.de>
LABEL Description="This image allows you to use play your sound files in a mumble channel through a client controlled via web interface"

#RUN echo https://dl-cdn.alpinelinux.org/alpine/latest-stable/main | tee /etc/apk/repositories \
#  && echo https://dl-cdn.alpinelinux.org/alpine/latest-stable/community | tee -a /etc/apk/repositories

# Install dependencies
RUN set -ex
                # packages for gomumblesoundboard
RUN     apk --no-cache add \
		tmux \
		ffmpeg \
		musl-dev \
		opus \
		go \
		opus-dev

# start getting going (hah)
RUN adduser -D msb
USER msb
WORKDIR /home/msb/

## get feuerrot's, i mean, foxpanther's fork of feuerrots fork of robbi5's gomumblesoundboard.
RUN go clean --cache \
        && go install github.com/foxpanther/gomumblesoundboard@latest

#Remove packages for space saving - I'm sure I could squeeze out more
#RUN apk --no-cache del git opus-dev musl-dev pkgconf
        #rm -rf /var/cache/apk/*

## Mount msb-sounds folder - user action required
# It's recommended that you mount a host folder or docker volume with sound files.
#
# Your local volume / data container is expected to have /sounds and /db as subfolders
# mount on docker run: -v <path on host / docker volume>:/media/msb
# put your audio files into /sounds, the /db file can be generated as described above
VOLUME ["/home/msb/sounds/"]

# define environment variables - default to them if nothing is declared on runtime
# ENV
ENV mumble_server=${mumble_server:-$MUMBLE_SERVER_PORT_64738_TCP_ADDR} \
 mumble_server_port=${mumble_server_port:-$MUMBLE_SERVER_PORT_64738_TCP_PORT} \
 mumble_server_channel=${mumble_server_channel:-Root} \
 mumble_user=${mumble_user:-spammer} \
 mumble_password=${mumble_password:-}

## Expose the port for the webserver - user action required
# Call the site with port 3000 for the raw gomumblesoundboard output
# Start the container with, e.g. -p 3001:80 if other web services are running on the
# host
EXPOSE 3000

# start
CMD [ "sh", "-c", "/home/msb/go/bin/gomumblesoundboard --username $mumble_user --server $mumble_server:$mumble_server_port --insecure --channel $mumble_server_channel --password $mumble_password /home/msb/sounds" ]
