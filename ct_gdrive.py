#!/usr/bin/python
#
# Google Drive Lustre/HSM lhsmtool_cmd copytool companion
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
ct_gdrive

Google Drive copytool companion for lhsmtool_cmd Lustre/HSM agent

Uses the Google Drive APIv3 Client Library (Apache License 2.0) to
archive/restore a Lustre file by FID to/from a Google Drive account.
"""

import argparse
from functools import wraps
import httplib
import httplib2
import logging
import random
import os
import re
import simplejson
import socket
from subprocess import Popen, PIPE
import sys
import time

# Google API imports
from apiclient import discovery
import oauth2client
from oauth2client import tools

from googleapiclient.http import MediaIoBaseDownload, MediaIoBaseUpload
from googleapiclient.errors import HttpError


# On-disk credentials filename (see ct_gdrive_oauth2.py)
OAUTH2_STORAGE_CREDS_FILENAME = 'ct_gdrive_creds.json'

# Our own exponential backoff max sleep time in seconds
MAX_EXPONENTIAL_SLEEP_SECS = 2100

# Values passed to the Drive API

# Integer, number of times to retry 500's with randomized exponential
# backoff. If all retries fail, the raised HttpError represents the
# last request. If zero (default), we attempt the only once.
# Note: 500 errors are logged with log_level=WARNING
GAPI_500_NUM_RETRIES = 10

# Files larger than 256 KB (256 * 1024 B) must have chunk sizes that
# are multiples of 256 KB.
GAPI_MEDIA_IO_CHUNK_SIZE = 16*1024*1024


def get_parser():
    """lhsm_gdrive command line options"""
    parser = argparse.ArgumentParser(add_help=False)
    # commands
    parser.add_argument('--action', choices=('pull', 'push'), required=True)
    # mandatory fd
    parser.add_argument('--fd', type=int, required=True)
    # mandatory Lustre fid
    parser.add_argument('--fid', required=True)
    # Lustre mount point
    parser.add_argument("--lustre-root", required=True)
    # Gdrive root ID
    parser.add_argument("--gdrive-root", required=True)
    # Credentials directory
    parser.add_argument("--creds-dir", required=True)

    return argparse.ArgumentParser(parents=[tools.argparser, parser])

args = get_parser().parse_args()

# oauth2 client: do not run a local web server
args.noauth_local_webserver = True


def oauth2_drive_service():
    """
    Used to get valid user credentials from storage that returns a
    Google Drive service object with authorized API access.

    Do NOT perform OAuth2 flow to obtain new credentials if nothing has
    been stored, or if the stored credentials are invalid. Use another
    script named ct_gdrive_oauth2.py for that.
    """
    # Get credentials from storage
    creds_path = os.path.join(args.creds_dir, OAUTH2_STORAGE_CREDS_FILENAME)

    credentials = oauth2client.file.Storage(creds_path).get()

    if not credentials or credentials.invalid:
        raise Exception('Unauthorized Access!')

    # Authorize http requests
    http = credentials.authorize(httplib2.Http())

    # Return an authorized Drive APIv3 service object
    return discovery.build('drive', 'v3', http=http)


def exponential_backoff(func):
    """
    Decorator used to implement a randomized exponential backoff retry
    strategy for API requests, allowing the use of many copytools to
    transfer data to/from Google as fast as possible.
    """
    @wraps(func)
    def wrapper(*fargs, **fkwargs):
        """wrapper function"""
        attempt_cnt = 0
        while True:
            try:
                return func(*fargs, **fkwargs)
            except (socket.error, HttpError, httplib.BadStatusLine), exc:
                logger = logging.getLogger(__name__)
                func_name = func.__name__
                logger.error("%s: %s", func_name, exc)

                if hasattr(exc, 'content'):
                    error = simplejson.loads(exc.content).get('error')
                    code = error.get('code')
                    message = error.get('message', '')
                    if code != 403 and code < 500:
                        logger.error("%s: Http fatal error %s (%s)", func_name,
                                     code, message)
                        raise # we want to see the full backtrace

                attempt_cnt += 1
                exp_sleep_secs = (2 ** attempt_cnt) + \
                                 float(random.randint(0, 1000)) / 1000.0

                if exp_sleep_secs > MAX_EXPONENTIAL_SLEEP_SECS:
                    logger.error("%s: aborting exponential backoff", func_name)
                    raise

                logger.warning("%s: sleeping %s secs", func_name,
                               exp_sleep_secs)
                time.sleep(exp_sleep_secs)
                logger.info("%s: now retrying", func_name)
    return wrapper

@exponential_backoff
def drive_list_files(query, service):
    """List Drive file IDs matching the given query (w/o pagination)"""
    return service.files() \
                  .list(q=query, fields="files(id)") \
                  .execute(num_retries=GAPI_500_NUM_RETRIES) \
                  .get('files', [])

def drive_lookup(parent, name, service):
    """Retrieve Google Drive file ID by parentID and name"""
    query_fmt = "'%s' in parents and name = '%s' and trashed = false"
    query = query_fmt % (parent, name)
    return drive_list_files(query=query, service=service)

def description_by_fid(lustre_fid):
    """
    This is what we use as the file description in Google Drive at Stanford
    Research Computing. This is probably to be improved. It is good to have
    a human readable file path, but you can put whatever you want here.
    """
    cmd_descr_fmt = 'lfs fid2path "%s" "%s"; ' \
                    'stat %s/.lustre/fid/%s; ' \
                    'echo Archived by $HOSTNAME on $(date)'

    cmd_descr = cmd_descr_fmt % (args.lustre_root, lustre_fid,
                                 args.lustre_root, lustre_fid)

    return Popen(cmd_descr, stdout=PIPE, shell=True).communicate()[0]

#
# GDrive push functions
#
@exponential_backoff
def drive_push_create_media(body, media, service):
    """Create a new file in Google Drive and upload media"""
    return service.files() \
                  .create(body=body, media_body=media) \
                  .execute(num_retries=GAPI_500_NUM_RETRIES)

def drive_push_create(service, lustre_fid):
    """Push a new file to Google Drive"""
    logger = logging.getLogger(__name__)
    logger.debug("drive_push_create lustre_fid %s from fd %d", lustre_fid,
                 args.fd)

    body = {'mimeType': 'application/octet-stream',
            'name': lustre_fid,
            'description': description_by_fid(lustre_fid),
            'parents': [args.gdrive_root]}

    # Open a Python file based on inherited Lustre file descriptor
    with os.fdopen(args.fd, 'r') as lustre_file:
        # Upload content directly using Lustre file
        media = MediaIoBaseUpload(lustre_file,
                                  mimetype='application/octet-stream',
                                  chunksize=GAPI_MEDIA_IO_CHUNK_SIZE,
                                  resumable=True)

        return drive_push_create_media(body=body, media=media, service=service)

@exponential_backoff
def drive_push_upload_media(drive_fid, body, media, service):
    """Upload a new version of the given Google Drive file"""
    return service.files() \
                  .update(fileId=drive_fid, body=body, media_body=media) \
                  .execute(num_retries=GAPI_500_NUM_RETRIES)

def drive_push_update(lustre_fid, drive_fid, service):
    """Push a new version of file to Google Drive"""
    logger = logging.getLogger(__name__)
    logger.debug("drive_push_update drive_fid %s for lustre_fid %s from fd %d",
                 drive_fid, lustre_fid, args.fd)

    body = {'mimeType': 'application/octet-stream',
            'description': description_by_fid(lustre_fid)}

    media = MediaIoBaseUpload(os.fdopen(args.fd, 'r'),
                              mimetype='application/octet-stream',
                              chunksize=GAPI_MEDIA_IO_CHUNK_SIZE,
                              resumable=True)

    return drive_push_upload_media(drive_fid, body, media, service)

def ct_gdrive_push(lustre_fid, service):
    """Main method to push/archive a file to Google Drive"""
    logger = logging.getLogger(__name__)
    logger.debug("ct_gdrive_push lustre_fid %s from fd %s", lustre_fid, args.fd)

    # A lookup is costly but REQUIRED to know whether a file with the
    # same _Lustre_FID_ name already exists in Google Drive.
    files = drive_lookup(parent=args.gdrive_root, name=lustre_fid,
                         service=service)

    if len(files) == 0:
        # File by Lustre FID not found: push a new file
        return drive_push_create(lustre_fid=lustre_fid, service=service)
    else:
        if len(files) > 1:
            logger.warning("multiple entries found for lustre_fid %s %s",
                           lustre_fid, files)

        # File already archived: push a new version of file
        return drive_push_update(lustre_fid=lustre_fid,
                                 drive_fid=files[0]['id'],
                                 service=service)

#
# GDrive pull functions
#
@exponential_backoff
def drive_pull_media(drive_fid, service):
    """Retrieve content of a Google Drive file"""
    logger = logging.getLogger(__name__)

    # Open a Python file based on inherited Lustre file descriptor
    with os.fdopen(args.fd, 'wb') as lustre_file:
        # Get a file content by Google fileID
        request = service.files().get_media(fileId=drive_fid)

        downloader = MediaIoBaseDownload(lustre_file,
                                         request,
                                         chunksize=GAPI_MEDIA_IO_CHUNK_SIZE)

        # Download by chunk
        status, done = downloader.next_chunk()
        while done is False:
            status, done = downloader.next_chunk()
            if status:
                logger.debug("Download %d%%", int(status.progress() * 100))

def ct_gdrive_pull(lustre_fid, service):
    """Main method to pull/restore a file from Google Drive"""
    logger = logging.getLogger(__name__)
    logger.debug("ct_gdrive_pull lustre_fid %s to fd %s", lustre_fid, args.fd)

    files = drive_lookup(parent=args.gdrive_root, name=lustre_fid,
                         service=service)
    if len(files) == 0:
        logger.error("ct_gdrive_pull: entry for lustre_fid %s not found!",
                     lustre_fid)
        sys.exit(1)

    else:
        if len(files) > 1:
            logger.warning("multiple entries found for lustre_fid %s %s",
                           lustre_fid, files)

        drive_pull_media(drive_fid=files[0]['id'], service=service)

#
# main ct_gdrive
#
def main():
    """main ct_gdrive.py entry point"""

    # Set logging level
    nloglevel = getattr(logging, args.logging_level.upper(), None)
    if not isinstance(nloglevel, int):
        raise ValueError('Invalid log level: %s' % nloglevel)

    # Log to stderr (will go through lhsmtool_cmd stderr)
    logging.basicConfig(level=nloglevel,
                        format='%(asctime)s %(name)s %(levelname)s %(message)s',
                        stream=sys.stderr)
    logger = logging.getLogger(__name__)

    try:
        # clean Lustre FID (no braces)
        expr = r'^\[?(0x[\da-f]+:0x[\da-f]+:0x[\da-f]+)\]?$'
        fid_clean, = re.match(expr, args.fid).groups()
    except AttributeError:
        logger.error("malformed lustre fid: %s", args.fid)
        return 1

    # Run action
    if args.action == 'push':
        response = ct_gdrive_push(fid_clean, oauth2_drive_service())
        logger.debug("push successfully completed for %s (drive_fid %s)",
                     fid_clean, response['id'])
        return 0
    elif args.action == 'pull':
        ct_gdrive_pull(fid_clean, oauth2_drive_service())
        logger.debug("pull succesfully completed for %s", fid_clean)
        return 0

    return 1

if __name__ == '__main__':
    sys.exit(main())
