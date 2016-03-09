#!/bin/sh

## init.sh file for starting and controlling the application inside
# gymnae/mumblesoundboard


## Optional for tty start instead of starting with an env-file
#
# Request input from user upon start
# You can comment this section back in and use it for an interactive containter start
#read -t 7 -p "Server (mumble_server container on host): " mumble_server
#read -t 7 -p "Server Port (64738): " mumble_server_port
#read -t 7 -p "Channel to spam (Root): " mumble_server_channel
#read -t 7 -p "User name for spammer (spammer): " mumble_user
#read -t 7 -sp "Password to enter server (no password): " mumble_password

# no changes below this line
#---------------------------------------
#

## Define variables with standard values where useful
# this script is tuned ot be run in conjunction with a docker container
# running a mumble server named mumble-server (e.g. gymnae/mumbleserver)
# you can also use any local or remote mumble server
#
# Please note that mumble only allows nicknames once per server
# non-standart input is expected from 'read' via shell or an env-file
# env-file is standard
mumble_server=${mumble_server:-$MUMBLE_SERVER_PORT_64738_TCP_ADDR}
mumble_server_port=${_mumble_server_port:-$MUMBLE_SERVER_PORT_64738_TCP_PORT}
mumble_server_channel=${mumble_server_channel:-Root}
mumble_user=${mumble_user:-spammer}
mumble_password=${mumble_password:-}

# start php-fpm
mkdir -p /tmp/logs/php-fpm
php-fpm

# start nginx
mkdir -p /tmp/logs/nginx
mkdir -p /tmp/nginx
chown nginx /tmp/nginx
nginx

# start and control the gomumblesoundserver
cd /go/gomumblesoundboard

tmux new -d -s uberspammer ./gomumblesoundboard --username "$mumble_user" --server "$mumble_server":"$mumble_server_port" --insecure --channel "$mumble_server_channel" --password "$mumble_password" /media/msb/sounds/*

# in case the script is called with either stop or restart
if [ $# -ge 1 ]; then
	
	if [ $1 = "stop" ]; then
		tmux kill-session -t uberspammer
	fi

	if [ $1 = "restart" ]; then
		tmux kill-session -t uberspammer
		tmux new -d -s uberspammer ./gomumblesoundboard --username "$mumble_user" --server "$mumble_server":"$mumble_server_port" --insecure --channel "$mumble_server_channel" --password "$mumble_password" /media/msb/sounds/*
	fi

else
# in case the script is called without any argument - just for keeping docker
# deamon from closing the container we'll display top. not the best, but works
	top
	exit 1
fi

