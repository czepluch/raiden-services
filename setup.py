#!/usr/bin/env python3

import io
import os
from glob import glob
from os.path import basename, dirname, join, splitext

from setuptools import find_packages, setup

REQ_REPLACE = {
    'git+https://github.com/matrix-org/matrix-python-sdk.git': 'matrix-client',
    'git+https://github.com/raiden-network/raiden.git@7827c0385a0af808b40e3c3217c0c243ed24e097': 'raiden',
}

DESCRIPTION = 'Raiden Services contain additional tools for the Raiden Network.'


def read_requirements(path: str):
    assert os.path.isfile(path)
    ret = []
    with open(path) as requirements:
        for line in requirements.readlines():
            line = line.strip()
            if line and line[0] in ('#', '-'):
                continue
            if line in REQ_REPLACE.keys():
                line = REQ_REPLACE[line]
            ret.append(line)

    return ret


def read(*names, **kwargs):
    return io.open(
        join(dirname(__file__), *names),
        encoding=kwargs.get('encoding', 'utf8'),
    ).read()


setup(
    name='raiden-services',
    version='0.0.1',
    license='MIT',
    description=DESCRIPTION,
    author='Brainbot Labs Est.',
    author_email='contact@brainbot.li',
    url='https://github.com/raiden-network/raiden-services',
    packages=find_packages('src'),
    package_dir={'': 'src'},
    py_modules=[splitext(basename(path))[0] for path in glob('src/*.py')],
    include_package_data=True,
    zip_safe=False,
    classifiers=[
        # complete classifier list: http://pypi.python.org/pypi?%3Aaction=list_classifiers
        'Development Status :: 3 - Alpha',
        'Intended Audience :: Developers',
        'License :: OSI Approved :: MIT License',
        'Natural Language :: English',
        'Programming Language :: Python :: 3',
        'Programming Language :: Python :: 3.7',
    ],
    keywords=[
        'raiden', 'ethereum', 'blockchain',
    ],
    install_requires=[
        read_requirements('requirements.txt'),
    ],
    extras_require={
        'dev': read_requirements('requirements-dev.txt'),
    },
    entry_points={
        'console_scripts': [
            'pathfinding-service=pathfinding_service.cli:main',
            'claim-pfs-fees=pathfinding_service.claim_fees:main',
            'monitoring-service=monitoring_service.cli:main',
            'request-collector=request_collector.cli:main',
        ],
    },
)
