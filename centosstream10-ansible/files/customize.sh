#!/bin/bash
# Update packages to the latest version
####
# dnf check-update returns an exit code of 100 if updates are available \
# or 0 if no updates are available and a non-zero for errors. \
# We can run the update command if the exit code is 100. \
if ! dnf check-update; then
  if [ $? -eq 100 ]; then
    dnf -y update
  else
    echo "An error occurred or no packages to upgrade. Continuing..."
  fi
else
  echo "No packages to upgrade. Continuing..."
fi
# Remove packages that are nolonger requried.
# Clean the dnf cache.
dnf -y autoremove
dnf clean all
rm -rf /var/cache/dnf/*

# Configure systemd.
# See https://hub.docker.com/_/centos/ for details.
(cd /lib/systemd/system/sysinit.target.wants/; for i in *; do [ $i == systemd-tmpfiles-setup.service ] || rm -f $i; done) ;\
rm -f /lib/systemd/system/multi-user.target.wants/* ;\
rm -f /etc/systemd/system/*.wants/* ;\
rm -f /lib/systemd/system/local-fs.target.wants/* ;\
rm -f /lib/systemd/system/sockets.target.wants/*udev* ;\
rm -f /lib/systemd/system/sockets.target.wants/*initctl* ;\
rm -f /lib/systemd/system/basic.target.wants/* ;\
rm -f /lib/systemd/system/anaconda.target.wants/*

#      # Upgrade pip.
#    - RUN python3 -m pip install --upgrade pip \
#      && python3 -m pip cache purge
#
#    # Install ansible.
#    - RUN python3 -m pip install ansible \
#      && python3 -m pip cache purge

#Create ansible directory and copy ansible inventory file.
mkdir /etc/ansible \
&& echo -e '[local]\nlocalhost ansible_connection=local' > /etc/ansible/hosts
