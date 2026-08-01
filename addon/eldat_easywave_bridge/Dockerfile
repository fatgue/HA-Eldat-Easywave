ARG BUILD_FROM
FROM ${BUILD_FROM}

# libusb is what makes the userspace CP210x path possible. It is deliberately
# installed here rather than relied upon from the Home Assistant image, which
# does not ship it -- that is precisely why the USB handling lives in this
# add-on instead of in the integration.
#
# pyserial is only used on the kernel path, when the stick did get a tty.
RUN apk add --no-cache \
        libusb \
        py3-pip \
        python3 \
    && pip3 install --break-system-packages --no-cache-dir \
        pyusb==1.2.1 \
        pyserial==3.5

WORKDIR /app
COPY bridge/ /app/bridge/
COPY run.sh /run.sh
RUN chmod a+x /run.sh

CMD ["/run.sh"]
