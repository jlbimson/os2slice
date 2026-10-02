# OrcaSlicer's desktop app streamed to the browser (linuxserver.io, Selkies), with the
# os2slice inbox: parts sent from the Onshape panel open in the running window.
# Built with the addon/ folder as context (docker-compose.yml), to share the inbox
# service with the web Bambu Studio. Pinned: upgrade by changing the tag.
FROM lscr.io/linuxserver/orcaslicer:v2.4.2-ls42
ENV START_DOCKER=false \
    OS2SLICE_APP=/opt/orcaslicer/AppRun \
    OS2SLICE_APP_PROC=bin/orca-slicer \
    OS2SLICE_APP_NAME=OrcaSlicer \
    OS2SLICE_APP_CONF=/config/.config/OrcaSlicer/OrcaSlicer.conf \
    OS2SLICE_INBOX=/share/os2slice/orca-inbox \
    OS2SLICE_STATUS=/share/os2slice/web-orca.json \
    OS2SLICE_DIAG_PREFIX=/share/os2slice/web-orca
# The image maximizes every window, dialogs too: maximize normal windows only (as in
# bambustudio_web/Dockerfile).
RUN grep -q '<windowRule identifier="\*"><action name="Maximize" /></windowRule>' /defaults/labwc.xml \
 && sed -i 's|<windowRule identifier="\*"><action name="Maximize" /></windowRule>|<windowRule identifier="*" type="normal"><action name="Maximize" /></windowRule>|' /defaults/labwc.xml \
 && grep -q 'type="normal"><action name="Maximize"' /defaults/labwc.xml
COPY bambustudio_web/root/usr/local/bin/os2slice-inbox /usr/local/bin/os2slice-inbox
COPY orcaslicer_web/root/ /
RUN chmod 755 /custom-cont-init.d/*.sh /custom-services.d/* /usr/local/bin/os2slice-inbox \
 && chown -R root:root /custom-cont-init.d /custom-services.d
