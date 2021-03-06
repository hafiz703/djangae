#!/usr/bin/env python
import os
import stat
import shutil
import subprocess
import sys

import tarfile
from StringIO import StringIO
from zipfile import ZipFile
from urllib import urlopen


PROJECT_DIR = os.path.abspath(os.path.dirname(__file__))
REQUIREMENTS_FILE = os.path.join(PROJECT_DIR, "requirements.txt")
TARGET_DIR = os.path.join(PROJECT_DIR, "libs")

APPENGINE_TARGET_DIR = os.path.join(TARGET_DIR, "google_appengine")

DJANGO_VERSION = os.environ.get("DJANGO_VERSION", "1.8")
APPENGINE_SDK_VERSION = os.environ.get("SDK_VERSION", "1.9.40")
APPENGINE_SDK_FILENAME = "google_appengine_%s.zip" % APPENGINE_SDK_VERSION
INSTALL_APPENGINE_SDK = "--install_sdk" in sys.argv

# Google move versions from 'featured' to 'deprecated' when they bring
# out new releases
FEATURED_SDK_REPO = "https://storage.googleapis.com/appengine-sdks/featured/"
DEPRECATED_SDK_REPO = "https://storage.googleapis.com/appengine-sdks/deprecated/%s/" % APPENGINE_SDK_VERSION.replace('.', '')

DJANGO_VERSION = os.environ.get("DJANGO_VERSION", "1.8")


if any([x in DJANGO_VERSION for x in ['master', 'a', 'b', 'rc']]):
    # For master, beta, alpha or rc versions, get exact versions
    DJANGO_FOR_PIP = "https://github.com/django/django/archive/{}.tar.gz".format(DJANGO_VERSION)
else:
    # For normal (eg. 1.8, 1.9) releases, get latest (.x)
    DJANGO_FOR_PIP = "https://github.com/django/django/archive/stable/{}.x.tar.gz".format(DJANGO_VERSION)

if __name__ == '__main__':

    if INSTALL_APPENGINE_SDK or not os.path.exists(APPENGINE_TARGET_DIR):

        # If we're going to install the App Engine SDK then we can just wipe the entire TARGET_DIR
        if os.path.exists(TARGET_DIR):
            shutil.rmtree(TARGET_DIR)

        print('Downloading the AppEngine SDK...')

        #First try and get it from the 'featured' folder
        sdk_file = urlopen(FEATURED_SDK_REPO + APPENGINE_SDK_FILENAME)
        if sdk_file.getcode() == 404:
            #Failing that, 'deprecated'
            sdk_file = urlopen(DEPRECATED_SDK_REPO + APPENGINE_SDK_FILENAME)

        #Handle other errors
        if sdk_file.getcode() >= 299:
            raise Exception('App Engine SDK could not be found. {} returned code {}.'.format(sdk_file.geturl(), sdk_file.getcode()))

        zipfile = ZipFile(StringIO(sdk_file.read()))
        zipfile.extractall(TARGET_DIR)

        #Make sure the dev_appserver and appcfg are executable
        for module in ("dev_appserver.py", "appcfg.py"):
            app = os.path.join(APPENGINE_TARGET_DIR, module)
            st = os.stat(app)
            os.chmod(app, st.st_mode | stat.S_IEXEC)
    else:
        print('Not updating SDK as it exists. Pass --install_sdk to install it.')
        # In this sencario we need to wipe everything except the SDK from the TARGET_DIR
        for name in os.listdir(TARGET_DIR):
            path = os.path.join(TARGET_DIR, name)
            if path == APPENGINE_TARGET_DIR:
                continue
            shutil.rmtree(path)

    print("Running pip...")
    args = ["pip", "install", "--no-deps", "-r", REQUIREMENTS_FILE, "-t", TARGET_DIR, "-I"]
    p = subprocess.Popen(args)
    p.wait()

    print("Installing Django {}".format(DJANGO_VERSION))
    args = ["pip", "install", "--no-deps", DJANGO_FOR_PIP, "-t", TARGET_DIR, "-I", "--no-use-wheel"]
    p = subprocess.Popen(args)
    p.wait()

    print("Installing Django tests from {}".format(DJANGO_VERSION))
    django_tgz = urlopen(DJANGO_FOR_PIP)

    tar_file = tarfile.open(fileobj=StringIO(django_tgz.read()))
    for filename in tar_file.getnames():
        if filename.startswith("django-stable-{}.x/tests/".format(DJANGO_VERSION)) or \
                filename.startswith("django-master/tests/") or \
                filename.startswith("django-{}/tests/".format(DJANGO_VERSION)):
            tar_file.extract(filename, os.path.join(TARGET_DIR))
