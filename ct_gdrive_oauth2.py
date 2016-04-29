#!/usr/bin/python
#
# Google Drive Lustre/HSM lhsmtool_cmd copytool companion
# OAuth2 Credentials helper script
#
# Written by Stephane Thiell <sthiell@stanford.edu>
#            Stanford Research Computing
#
# Copyright 2016
#     The Board of Trustees of the Leland Stanford Junior University
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with Foobar.  If not, see <http://www.gnu.org/licenses/>.

"""
ct_gdrive_oauth2.py

Perform OAuth2 Flow to store credentials for ct_gdrive.py
"""

from __future__ import print_function

import argparse
import os
import sys

from apiclient import discovery
import oauth2client
from oauth2client import client
from oauth2client import tools


# Google Drive API application name
APPLICATION_NAME = 'ct_gdrive'

# drive.file is per-file access to files created or opened by the app
SCOPES = 'https://www.googleapis.com/auth/drive.file'

# on-disk credentials filename
OAUTH2_STORAGE_CREDS_FILENAME = 'ct_gdrive_creds.json'


def get_parser():
    """ct_gdrive command line options"""
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--client-secret", required=True)
    # Credentials directory
    parser.add_argument("--creds-dir", required=True)

    return argparse.ArgumentParser(parents=[tools.argparser, parser])

args = get_parser().parse_args()

# oauth2 client: do not run a local web server
args.noauth_local_webserver = True

def new_credentials():
    """Perform OAuth2 flow to obtain the new credentials.

    Return:
        Credentials, the obtained credential.
    """
    credential_path = os.path.join(args.creds_dir, 'ct_gdrive_creds.json')

    store = oauth2client.file.Storage(credential_path)
    credentials = store.get()

    if not credentials or credentials.invalid:
        flow = client.flow_from_clientsecrets(args.client_secret, SCOPES)
        flow.user_agent = APPLICATION_NAME
        credentials = tools.run_flow(flow, store, args)
        print('Storing credentials to ' + credential_path)
    return credentials

def main():
    """main ct_gdrive_oauth2 entry point"""
    creds = new_credentials()
    return 0

if __name__ == '__main__':
    sys.exit(main())
