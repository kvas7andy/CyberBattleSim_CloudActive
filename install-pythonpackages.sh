#!/bin/bash

# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

set -ex

. ./getpythonpath.sh

export MPLLOCALFREETYPE=1

# Install python packages
$PYTHON -m pip install --upgrade pip
$PYTHON -m pip install --upgrade setuptools==65.5.0 # specific version needed for gym==0.21.0
$PYTHON -m pip install --upgrade distlib
$PYTHON -m pip install wheel
$PYTHON -m pip install -e .
$PYTHON -m pip install -e .[dev]
$PYTHON -m pip install jupyter_contrib_nbextensions && jupyter contrib nbextension install 

if [ ""$GITHUB_ACTION"" == "" ]; then
  echo 'Install the `pre-commit` package (running on a dev box and not under GitHub Actions)'
  $PYTHON -m pip install pre-commit
fi
