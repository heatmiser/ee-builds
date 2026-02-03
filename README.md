# Build execution environments with `ansible-builder`
## Building execution environment images for use with [Ansible Automation Platform 2](https://www.ansible.com/products/automation-platform)
### This repo responds to modifications on `main` and pull requests to main by building new container images with Github Actions to be used as execution environments for Ansible Automation Platform 2.

[![Centos Stream 10 Ansible EE build](https://github.com/heatmiser/ee-builds/actions/workflows/centosstream10-ansible-build.yml/badge.svg)](https://github.com/heatmiser/ee-builds/actions/workflows/centosstream10-ansible-build.yml) [![Windows EE build](https://github.com/heatmiser/ee-builds/actions/workflows/windows-ee-build.yml/badge.svg)](https://github.com/heatmiser/ee-builds/actions/workflows/windows-ee-build.yml) [![f5 EE build](https://github.com/heatmiser/ee-builds/actions/workflows/f5-ee-build.yml/badge.svg)](https://github.com/heatmiser/ee-builds/actions/workflows/f5-ee-build.yml) [![RHEL 90 min EE build](https://github.com/heatmiser/ee-builds/actions/workflows/rhel_90-ee.yml/badge.svg?branch=main)](https://github.com/heatmiser/ee-builds/actions/workflows/rhel_90-ee.yml)

![workflows](https://github.com/heatmiser/ee-builds/actions)


### Contributions
The directories in this repository should follow the format that `ansible-builder` expects. See [centosstream10-ansible](https://github.com/heatmiser/ee-builds/tree/main/centosstream10-ansible) as an example. The name you give the parent directory should also be the name of the image. You can also copy the [centosstream10-ansible workflow](https://github.com/heatmiser/ee-builds/blob/main/.github/workflows/centosstream10-ansible-build.yml) file and adjust the parameters. Questions? Open an issue!


### Useful documentation and links
- [Ansible Automation Platform](https://www.ansible.com/products/automation-platform)
- [ansible-navigator](https://github.com/ansible/ansible-navigator)
- [ansible-builder](https://github.com/ansible/ansible-builder)
- [GitHub Actions quickstart](https://docs.github.com/en/actions/quickstart)
- [GitHub environments](https://docs.github.com/en/actions/deployment/using-environments-for-deployment)
- [redhat-actions/podman-login](https://github.com/redhat-actions/podman-login)
- [redhat-actions/push-to-registry](https://github.com/redhat-actions/push-to-registry)

### Open Source
This project is based on the work of:
- @cloin and the original [ee-builds](https://github.com/cloin/ee-builds)project. Thank you, Colin!
- The Red Hat Ansible TMM [ee-builds](https://github.com/ansible-tmm/ee-builds) project.
- @ansiblejunky and the [Ansible Execution Environment](https://github.com/ansiblejunky/ansible-execution-environment) project.