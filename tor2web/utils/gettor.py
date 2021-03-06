"""

:mod:`Tor2Web`
=====================================================

.. automodule:: Tor2Web
   :synopsis: GetTor routines

.. moduleauthor:: Israel Leiva <ilv@torproject.org>
.. moduleauthor:: Giovanni Pellerano <evilaliv3@globaleaks.org>

"""

# -*- coding: utf-8 -*-

import os
import re

import json
import shutil

from distutils.version import LooseVersion

from twisted.internet import defer
from twisted.protocols.basic import FileSender
from twisted.python.filepath import FilePath
from twisted.python.log import err
from twisted.web.client import getPage, downloadPage
from twisted.web.server import NOT_DONE_YET

from tor2web.utils.lists import List
from tor2web.utils.ssl import HTTPSVerifyingContextFactory


REDIRECT_URLS = {
    'iphone': 'https://itunes.apple.com/us/app/onion-browser/id519296448',
    'android': 'https://play.google.com/store/apps/details?id=org.torproject.android'
}


def getRedirectURL(client):
    """Get the redirect URL for a given OS.

    Requests from iPhone, Android and Tor Browser itself should be
    redirected.

    :param: client (string) the user's operating system.

    :return: (string) the intended URL for redirection.

    """
    return REDIRECT_URLS[client]


def sendFile(request, filename, tb_path, ctype):
    """Send file to user.

    Send file to user using producers and consumers system.

    :param: filename (string)
    :param: filepath (string)
    :param: ctype (string) the value for content-type HTTP header

    """
    request.setHeader(b'content-type', ctype)
    request.setHeader(
        b'content-disposition', 'attachment; filename=%s' %
        filename
    )

    fp = FilePath(tb_path).child(filename).open()

    d = FileSender().beginFileTransfer(fp, request)

    def cbFinished(ignored):
        fp.close()
        request.finish()

    d.addErrback(err).addCallback(cbFinished)


def getBestLangMatch(accept_language, supported_lcs):
    """Return the best match between accept_language and supported_languages
    :param: accept_language (string) the accept language header
    :param: supported_lcs (array) the available TBB languages

    return: the best match with the languages supported by an user client
    """
    def parse_accept_language(accept_language):
        return [l.split(';')[0] for l in accept_language.replace(" ", "").split(',')]

    def language_only(lc):
        if '-' in lc:
            lc = lc.split('-')[0]

        return lc

    for lc in parse_accept_language(accept_language):
        # returns es-PT if es-PT is available (perfect match)
        for l in supported_lcs:
            if lc.lower() == l.lower():
                return l

        lc = language_only(lc)

        # returns es if asking for es-PT with
        # es-PT not available but es available
        for l in supported_lcs:
            if lc.lower() == l.lower():
                return l

        # returns es-ES if asking for es-PT with
        # es-PT and es not available but  es-ES available
        for l in supported_lcs:
            if lc.lower() == language_only(l).lower():
                return l

    return 'en-US' # last resort


def getOSandLC(headers, locales):
    """Get OS and LC of user.

    The user-agent and accept-language headers of the client's browser
    are used to guess the operating system and locale.

    :return: tuple of strings with the OS and LC.

    """
    agent = str(headers.getRawHeaders(b'user-agent'))
    alang = str(headers.getRawHeaders(b'accept-language'))

    client = None

    if re.search('Windows', agent):
        client = 'windows'

    elif re.search('Mac OS X', agent):
        client = 'osx'

    elif re.search('iPhone', agent):
        client = 'iphone'

    elif re.search('Android', agent):
        client = 'android'

    lang = getBestLangMatch(alang, locales)

    return client, lang


def processGetTorRequest(request, client, lang, type, version, t2w_tb_path):
    """Process a GetTor request.

    Determine the file needed and send it to the user. Only requests for
    windows and osx should call this.

    :param: request (object)
    :param: client (string) the user's operating system.
    :param: lang (string) the user's locale.
    :param: type (string) the type of request (file or signature).
    :param: version (string) the latest version of Tor Browser to be served.
    :param: t2w_tb_path (string): path to the latest Tor Browser files.

    """
    # windows and osx files have different names and extensions
    if client == 'windows':

        if type == 'signature':
            ext = 'exe.asc'
            ctype = 'application/pgp-signature'
        else:
            ext = 'exe'
            ctype = 'application/x-msdownload'

        tb_file = 'torbrowser-install-%s_%s.%s' % (version, lang, ext)

    elif client == 'osx':

        if type == 'signature':
            ext = 'dmg.asc'
            ctype = 'application/pgp-signature'
        else:
            ext = 'dmg'
            ctype = 'application/x-apple-diskimage'

        tb_file = 'TorBrowser-%s-osx32_%s.%s' % (version, lang, ext)

    sendFile(request, tb_file, t2w_tb_path, ctype)

    defer.returnValue(NOT_DONE_YET)


def getTBBVersions(url, sslContextFactory):
    """Return latests TBB versions

    :param: url (string) the official TBB release suggestions
    :param: sslContextFactory (object) factory to be used for certificate validation
    """
    return getPage(url, sslContextFactory)


def getTBBFilenames(url, urls_regexp, sslContextFactory):
    """Return filenames listed on TBB repository that match specified regexp

    :param: url (string) the TBB repository
    :param: urls_regexp (regexp) the url regexp pattern
    :param: sslContextFactory (object) factory to be used for certificate validation
    """
    def extractLinks(page, urls_regexp):
        matches = re.findall(urls_regexp, page)
        return set(tuple(x[0] for x in matches))

    d = getPage(url, sslContextFactory)
    d.addCallback(extractLinks, urls_regexp)
    return d


def getLatestTBBVersion(versions):
    """Return the latest TBB stable version among the version availables

    :param: versions (list) an array containing version numbers
    :return: return latest TBB stable version
    """
    version_numbers = []
    for v in versions:
        if '-' not in v:
            version_numbers.append(v)

    stable_version = version_numbers[0]
    for v in version_numbers:
        if LooseVersion(v) < LooseVersion(stable_version):
            stable_version = v

    return stable_version


@defer.inlineCallbacks
def getTorTask(config):
    """Script to fetch the latest Tor Browser versions.

    Fetch the latest versions of Tor Browser from dist.torproject.org.

    :param: config (object) The tor2web configuration
    """
    try:
        sslContextFactory1 = HTTPSVerifyingContextFactory('www.torproject.org')
        sslContextFactory2 = HTTPSVerifyingContextFactory('dist.torproject.org')

        # path to latest version of Tor Browser and Tor Browser files
        latest_tb_file = os.path.join(config.datadir, 'lists/latest_torbrowser.txt')
        save_path = os.path.join(config.datadir, 'torbrowser/')

        # server from which to download Tor Browser
        dist_tpo = 'https://dist.torproject.org/torbrowser/'

        # find out the latest version
        response = yield getTBBVersions("https://www.torproject.org/projects/torbrowser/RecommendedTBBVersions",
                                        sslContextFactory1)

        latest_version = getLatestTBBVersion(json.loads(response))

        # find out the current version delivered by GetTor static URL
        current_version = ""
        with open (latest_tb_file, 'r') as version_file:
            current_version = version_file.read().replace('\n', '')

        if current_version != latest_version:

            mirror = str('%s%s/' % (dist_tpo, latest_version))

            filenames_regexp = ''

            i = 0
            for lang in List(config.t2w_file_path('lists/gettor_locales.txt')):
                if i:
                    filenames_regexp += '|'

                filenames_regexp += "(%s.exe)|(%s.exe.asc)|(%s.dmg)|(%s.dmg.asc)" % (lang,
                                                                                     lang,
                                                                                     lang,
                                                                                     lang)
                i += 1

            url_regexp = 'href=[\'"]?([^\'" >]+(%s))' % filenames_regexp
            files = yield getTBBFilenames(mirror, url_regexp, sslContextFactory2)

            temp_path = os.path.join(save_path, 'temp')
            latest_path = os.path.join(save_path, 'latest')

            shutil.rmtree(temp_path, True)
            os.mkdir(temp_path)
            for f in files:
                url = str('%s%s/%s' % (dist_tpo, latest_version, f))
                savefile = FilePath(temp_path).child(f).open('w')
                yield downloadPage(url, savefile, sslContextFactory2)

            shutil.rmtree(latest_path, True)
            shutil.move(temp_path, latest_path)

            # if everything is OK, update the current version delivered by
            # GetTor static URL
            with open(latest_tb_file, 'w') as version_file:
                version_file.write(latest_version)

    except Exception:
        pass
