Mumblesoundboard
================

####An Alpine based docker image for playing sounds on Mumble Voicechat servers.

Making use of the following: 

 1. gomumblesoundboard base by: https://github.com/robbi5/gomumblesoundboard
		The MIT License (MIT)
 2. Webserver integration inspired by: https://github.com/gigablah/alpine-php
		The MIT License (MIT)	
 3. File manager: ELfinder - http://elfinder.org/#elf_l1_Lw
 		Copyright (c) 2009-2016, Studio 42
		All rights reserved.
 4. Soon: Additional web code by #freaks on EFnet - please see [note](#under-development)



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

##Preparations##
###Install via Docker###

    docker pull gymnae/mumblesoundboard:latest


###Prepare folders###
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

##Spin up a container##
*Example*

    docker run -d -p 3001:80 \
    --link mumble-server:mumble-server \
    -v msb-freaks:/media/msb/ \
    --env-file="freaks.env" \
    --name gomumblesoundboard \
    gymnae/mumblesoundboard:latest

###Recommended: Container linking###
`gymnae/mumblesoundboard` is designed to run linked to a running mumble server in another docker container. If you decide to do so, please make sure to use the alias `mumble-server`

You can also connect to an external server via an ENV variable, then you don't need linking:

###Enviroment variables####
Instead of an `--env-file` you can also pass ENV variables to the `run` command

    mumble_server         = <external mumble server if no server linked>
    mumble_server_channel = <defaulting to 'Root'>
    mumble_user           = <defaulting to 'Spammer'>
    mumble_password       = <optional, no default>
    mumble_server_port    = <defaulting to 64738>

###Ports###
Internally, the [gomumblesoundboard](https://github.com/robbi5/gomumblesoundboard%20%22gomumblesoundboard) base listens on port 3000 - so you could also `-p 3000:3000` when spinning up the container for direct control of the base

#####[Currently de-funct](#under-development):

>However, the main webserver listens on port 80.
>When spinning up different containers of this image, please remember to change the exterior ports.

----------

##Usage##
###Play sounds###
Just navigate to `http://<your webserver host>:<external port>` for a controlling web interface.

###Add sounds###
To make use of the integrated [elfinder](http://elfinder.org/#elf_l1_Lw "elfinder") navigate to 

`http://<your webserver host>:<external port>/fm/` 

There you directly upload to your [configured](#prepare-folders) persistent storage.

In oder to make use of new sounds you can either restart the container or call the init script:

    docker exec <containername> /init.sh restart

[Soon](#under-develpmoment): 
Alternatively, we can call a special script to restart the base
`http://<your webserver host>:<external port>/restart.php`

*(That is a sad workaround, I know. But the base doesn't allow a proper reload)*


----------
#####Under development
This part is currently under development, it will be reactivated once the code is ready. For now, please forward `-p 3000:3000`
