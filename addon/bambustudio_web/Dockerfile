# Bambu Studio's desktop app streamed to the browser (linuxserver.io, Selkies).
# Pinned: upgrade by changing the tag after checking the release notes.
FROM ubuntu:24.04 AS shim
RUN apt-get update && apt-get install -y --no-install-recommends gcc libc6-dev
COPY shim/dbus_path_fix.c /src/
RUN gcc -O2 -shared -fPIC -Wall -Werror -o /src/dbus_path_fix.so /src/dbus_path_fix.c -ldl

FROM lscr.io/linuxserver/bambustudio:v02.08.04.57-ls172
# No Docker-in-Docker: the add-on isn't privileged, and it only filled the log with errors.
ENV START_DOCKER=false
# Fixes Bambu Studio's invalid single-instance D-Bus path (see shim/dbus_path_fix.c), so
# files os2slice sends open in the running window.
COPY --from=shim /src/dbus_path_fix.so /usr/local/lib/os2slice/dbus_path_fix.so
RUN echo /usr/local/lib/os2slice/dbus_path_fix.so >> /etc/ld.so.preload
# The image maximizes every window, dialogs too ("open as project?" filled the screen).
# Maximize normal windows only; dialogs (labwc: a parent window or a fixed size) keep their
# size. The image copies this template to ~/.config/labwc/rc.xml at every start.
RUN grep -q '<windowRule identifier="\*"><action name="Maximize" /></windowRule>' /defaults/labwc.xml \
 && sed -i 's|<windowRule identifier="\*"><action name="Maximize" /></windowRule>|<windowRule identifier="*" type="normal"><action name="Maximize" /></windowRule>|' /defaults/labwc.xml \
 && grep -q 'type="normal"><action name="Maximize"' /defaults/labwc.xml
COPY root/ /
RUN chmod 755 /custom-cont-init.d/*.sh /custom-services.d/* /usr/local/bin/os2slice-inbox /usr/local/bin/os2slice-trust-bambuddy \
 && chown -R root:root /custom-cont-init.d /custom-services.d
