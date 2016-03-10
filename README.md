Mumblesoundboard
================

An Alpine based docker image for playing sounds on Mumble Voicechat servers.

Making use of the following: 

 1. gomumblesoundboard base by: https://github.com/robbi5/gomumblesoundboard
		The MIT License (MIT)
 2. Webserver integration inspired by: https://github.com/gigablah/alpine-php
		The MIT License (MIT)	
 3. File manager: ELfinder - http://elfinder.org/#elf_l1_Lw
 		Copyright (c) 2009-2016, Studio 42
		All rights reserved.
 4. Additional web code by #freaks on EFnet


----------

#### What is [mumble](https://wiki.mumble.info/wiki/Main_Page "mumble")?####
> "Mumble is an open source, low-latency, high quality voice chat software primarily intended for use while gaming."

- Low-latency - great for talking and gaming
- Stay private and secure
-- always encrypted communication
-- Public/private-key authentication by default
- Recognize friends across servers
- For gamers:
--In-game Overlay - see who is talking
-- Positional audio - hear the players from where they are located
- Wizards to guide you through setup, like configuring your microphone


----------


##Usage ##
####Install via Docker####

    docker pull gymnae/mumblesoundboard:latest


####Prepare folders####
Either create a volume

    docker volume create --name="<your great name>"

or directly prepare the necessary folder structure in your file system.

Either way, we need the following directory structure:
```
<foldername>/		 
   sounds/
   db/
```
You can fill `sounds/` with audio files as you like before spinning up or once the container is running - [see below](#add-sounds)
Just don't use subfolders. `db/` will be filled automatically.

Whenn spinning up a container, make sure to attach your folder: 
`-v </path/to/foldername>:/media/msb/`


----------


###Spin up a container###
*Example*

    docker run -d -p 3001:80 \
    --link mumble-server:mumble-server \
    -v msb-freaks:/media/msb/ \
    --env-file="freaks.env" \
    --name gomumblesoundboard \
    gymnae/mumblesoundboard:latest

####Recommended: Container linking####
`gymnae/mumblesoundboard` is designed to run linked to a running mumble server in another docker container. If you decide to do so, please make sure to use the alias `mumble-server`

You can also connect to an external server, then you don't ignore linking

####Enviroment variables#####
Instead of an `--env-file` you can also pass ENV variables to the `run` command

    mumble_server         = <external mumble server>
    mumble_server_channel = <defaulting to 'Root'>
    mumble_user           = <defaulting to 'Spammer'>
    mumble_password       = <optional, no default>
    mumble_server_port    = <defaulting to 64738>

####Ports####
Internally, the [gomumblesoundboard](https://github.com/robbi5/gomumblesoundboard%20%22gomumblesoundboard) base listens on port 3000 - so you could also `-p 3000:3000` when spinning up the container for direct control of the base

However, the main webserver listens on port 80.
When spinning up different containers of this image, please remember to change the exterior ports.


----------


###Usage###
####Play sounds####
Just navigate to `http://<your webserver host>:<external port>` for a controlling web interface.

####Add sounds####
Navigate to `http://<your webserver host>:<external port>/fm/` to make use of the integrated [elfinder](http://elfinder.org/#elf_l1_Lw "elfinder"). 

You can upload directly to your [configured](#prepare-folders) persistent storage.

In order to make use of newly added sounds we have to call a special script to restart the gomumblesoundboard base, or just restart the container.
`http://<your webserver host>:<external port>/restart.php`

*(That is a sad workaround, I know)*
