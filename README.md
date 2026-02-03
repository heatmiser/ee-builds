# Build execution environments with `ansible-builder`
## Building execution environment images for use with [Ansible Automation Platform 2](https://www.ansible.com/products/automation-platform)
### This repo responds to modifications on `main` and pull requests to main by building new container images with Github Actions to be used as execution environments for Ansible Automation Platform 2.

[Centos Stream 10 Ansible EE PR builds](https://github.com/heatmiser/ee-builds/pkgs/container/centosstream10-ansible)  [RHEL 90 min EE build](https://github.com/heatmiser/ee-builds/actions/workflows/rhel_90-ee.yml)  [Windows EE build](https://github.com/heatmiser/ee-builds/actions/workflows/windows-ee-build.yml)

[![Devel EE Build](https://github.com/heatmiser/ee-builds/actions/workflows/pr-ee-build.yml/badge.svg?branch=main)](https://github.com/heatmiser/ee-builds/actions/workflows/pr-ee-build.yml)
[![Prod EE Build](https://github.com/heatmiser/ee-builds/actions/workflows/push-ee-build.yml/badge.svg?branch=main)](https://github.com/heatmiser/ee-builds/actions/workflows/push-ee-build.yml)

[ee-builds GitHub Actions workflows](https://github.com/heatmiser/ee-builds/actions)


### Contributions
The directories in this repository should follow the format that `ansible-builder` expects. See [centosstream10-ansible](https://github.com/heatmiser/ee-builds/tree/main/centosstream10-ansible) as an example. The name you give the parent directory will be the name of the resulting image build. For the more advanced users, you can also copy one of the [GitHub  workflows](https://github.com/heatmiser/ee-builds/tree/main/.github/workflows) to a new workflow file and adjust the parameters. Have an idea for a new EE or a process improvement? Submit a PR! Any questions or comments?  Open an issue!


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
- [@cloin](https://www.github.com/cloin) and the original [ee-builds](https://github.com/cloin/ee-builds) project. Thank you, Colin!
- The Red Hat Ansible TMM [ee-builds](https://github.com/ansible-tmm/ee-builds) project.
- [@ansiblejunky](https://www.github.com/@ansiblejunky) and the [Ansible Execution Environment](https://github.com/ansiblejunky/ansible-execution-environment) project.