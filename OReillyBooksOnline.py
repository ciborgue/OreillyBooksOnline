#!/usr/bin/env python3
import asyncio
import aiohttp
import aiofiles
import aiofiles.os

import argparse
import json
import logging
import sqlite3
import random
import re
import os
import subprocess
import tempfile
import elementpath
from types import SimpleNamespace
from lxml import etree
from http import HTTPStatus
from urllib.parse import urlparse

import typing  # `pytype`s typing.Optional


class OreillyBooksOnline:
    CONST = SimpleNamespace(**{
        'LOGIN_ENDPOINT': 'https://learning.{oreilly}/profile/',
        'API_ENDPOINT': 'https://api.{oreilly}/api/v2/epubs/urn:orm:book:{book_id}/',
        'EPUB': 'EPUB',  # choises are {'OEBPS', 'OPS', 'EPUB'}
        'NSMAP_CHAPTER': {
            None: 'http://www.w3.org/1999/xhtml',
            'epub': 'http://www.idpf.org/2007/ops',
        },
        'NSMAP_CONTAINER': {
            None: 'urn:oasis:names:tc:opendocument:xmlns:container',
        },
        'ENCODING': 'utf-8',
    })
    HTTP_OK = [HTTPStatus.OK]

    def __init__(self,
                 args: argparse.Namespace) -> None:
        self.args = args

        logging.basicConfig(encoding=self.CONST.ENCODING,
                            level=getattr(logging, self.args.logging_level))

        self.root = f'{self.args.output}/{self.args.book_id}'
        logging.info(f'Root directory is set to: {self.root}')

        self.args.css_map = {
            (value := item.split(':'))[0]: value[1]
            for item in self.args.css_map.split(',') if item
        }

    @staticmethod
    async def _request(session,
                       url: str,
                       method: str = 'get',
                       data: dict = {}) -> SimpleNamespace:
        await asyncio.sleep(random.uniform(0.25, 1.00))
        async with getattr(session, method)(url) as resp:
            assert resp.status in OreillyBooksOnline.HTTP_OK, \
                f'Got {resp.status} (expected {OreillyBooksOnline.HTTP_OK}) for {url}'

            content_type = dict(zip(
                ['content', 'encoding'], re.split(r';\s*', resp.headers['Content-Type'])
            ))

            content_type['encoding'] = content_type.get(
                'encoding',
                f'encoding={OreillyBooksOnline.CONST.ENCODING}').split('=').pop()

            if content_type['content'] in ['application/json']:
                conv = ['read', 'json']
            else:
                conv = ['read']

            return SimpleNamespace(**(
                data | content_type |
                dict(zip(conv, [await getattr(resp, item)() for item in conv]))
            ))

    @staticmethod
    async def _write(epubpath: str, data: bytes) -> None:
        await aiofiles.os.makedirs(os.path.dirname(epubpath), exist_ok=True)

        async with aiofiles.open(epubpath, mode='wb') as handle:
            await handle.write(data)

    async def _patch(self,
                     book: SimpleNamespace,
                     asset: SimpleNamespace) -> typing.Optional[SimpleNamespace]:
        if asset.kind in ['image']:
            pass
        elif asset.kind in ['stylesheet']:
            if asset.full_path in self.args.css_map:
                logging.info(f'Replacing CSS content: {asset.full_path}'
                             f' with {self.args.css_map[asset.full_path]}')
                with open(self.args.css_map[asset.full_path], 'rb') as css:
                    asset.read = css.read()
        elif self.args.woff2 and \
                asset.full_path in {
                    item.full_path for item in book.assets
                    if item.kind in ['font', 'other_asset']
                    and len(media_type := item.media_type.split('/')) == 2
                    and media_type.pop() not in ['woff2']
                    and media_type.pop() in ['font']
                }:

            asset.inactive = True

            original_font = f'{tempfile.gettempdir()}/{asset.filename}'
            with open(original_font, 'wb') as font:
                font.write(asset.read)

            out = subprocess.run(['woff2_compress', original_font])
            assert out.returncode == 0, \
                f'woff2_compress returned {out.returncode}; MUST return zero'

            woff_asset = SimpleNamespace(**{
                item: re.sub(r'[/.](ttf|otf)$', '.woff2', asset.__dict__[item])
                for item in ['media_type', 'ourn', 'url', 'full_path', 'filename', 'filename_ext']
            })

            with open(f'{tempfile.gettempdir()}/{asset.filename}', 'rb') as woff:
                woff_asset.read = woff.read()

            return woff_asset
        elif asset.kind in ['chapter']:
            parser = etree.HTMLParser(encoding=asset.encoding)

            root = etree.fromstring(asset.read, parser)

            root.attrib.update({
                'lang': book.info['language'],
                'xml:lang': book.info['language'],
                'xmlns': self.CONST.NSMAP_CHAPTER[None],
                'xmlns:epub': self.CONST.NSMAP_CHAPTER['epub'],
            })

            head = etree.Element('head')
            root.insert(0, head)  # add <head> as a first element

            etree.SubElement(head, 'meta',
                             {'charset': asset.encoding,
                              'lang': book.info['language'],
                              'xml:lang': book.info['language']})

            chapter = next(item for item in book.chapters
                           if asset.url == item['content_url'])
            etree.SubElement(head, 'title').text = chapter['title']

            for css in chapter['related_assets']['stylesheets']:
                file = next(item for item in book.assets
                            if item.url == css)
                etree.SubElement(head, 'link', {'rel': 'stylesheet',
                                                'type': file.media_type,
                                                'href': file.full_path})

            for img in elementpath.select(root, r'//img[@src]'):
                src = urlparse(img.attrib['src'])
                if src.scheme not in ['http', 'https']:
                    file = next(item for item in book.files
                                if item['filename'] == os.path.basename(src.path))
                    img.attrib['src'] = src._replace(path=f'{file["full_path"]}',
                                                     scheme='', netloc='').geturl()

            for a in elementpath.select(root, r'//a[@href]'):
                href = urlparse(a.attrib['href'])
                if href.scheme not in ['http', 'https', 'mailto'] and href.path:
                    file = next(item for item in book.files
                                if item['filename'] == os.path.basename(href.path))
                    a.attrib['href'] = href._replace(path=f'{file["full_path"]}',
                                                     scheme='', netloc='').geturl()
            asset.read = self.etree_to_string(root)
        elif asset.kind in ['other_asset'] and \
                asset.media_type in ['application/oebps-package+xml']:
            parser = etree.XMLParser(encoding=asset.encoding)
            root = etree.fromstring(asset.read, parser)

            root.insert(0, etree.Comment('Prepared with: https://tinyl.io/Abuc'))

            for package_item in elementpath.select(root, r'//item[@href]'):
                # make it fail if font is not supplied in `book.assets`
                file = next(item for item in book.assets
                            if item.full_path == package_item.attrib['href']).full_path
                if self.args.woff2:
                    package_item.attrib['href'] = re.sub(r'[/.](ttf|otf)$', '.woff2', file)

            asset.read = self.etree_to_string(root)
        else:
            logging.info(f'No change: {asset.kind}:{asset.media_type}:{asset.full_path}')

    def retrieve_firefox_cookies(self) -> dict:
        cookie_jar = [
            f'{item[0]}/{self.args.cookie_file}'
            for item in os.walk(f'{os.environ["HOME"]}'
                                f'/Library/Application Support/Firefox/Profiles')
            if self.args.cookie_file in item[2]
        ]
        assert len(cookie_jar) == 1, "Your Firefox has multiple profiles; can't continue"

        with tempfile.NamedTemporaryFile() as db:
            os.system(f'dd if="{cookie_jar.pop()}" of="{db.name}" status=none')
            with sqlite3.connect(db.name) as connection:
                data = connection.cursor()
                return {
                    name: value for name, value, host in
                    [row for row in data.execute('SELECT name, value, host FROM moz_cookies')
                     if row[2].endswith(f'.{self.args.oreilly}')]
                }
        raise RuntimeError()

    async def check_login(self, session) -> None:
        url = self.CONST.LOGIN_ENDPOINT.format(oreilly=self.args.oreilly)
        assert self.args.email in \
            (await self._request(session, url)).read.decode(self.CONST.ENCODING), \
            f'Login failed: [{self.args.email}] is not in profile'

    def etree_to_string(self,
                        root: etree.Element,
                        encoding: typing.Optional[str] = None) -> bytes:
        return etree.tostring(
            root,
            encoding=encoding if encoding else self.CONST.ENCODING,
            xml_declaration=True,
            pretty_print=self.args.pretty_print
        )

    def generate_epub_mimetype(self):
        return SimpleNamespace(**{
            'full_path': '../mimetype',
            'read': b'application/epub+zip',
        })

    def generate_epub_container(self, book):
        package_opf = next(item for item in book.assets
                           if item.media_type == 'application/oebps-package+xml'
                           and item.full_path.endswith('.opf'))
        root = etree.Element('container',
                             nsmap=self.CONST.NSMAP_CONTAINER,
                             attrib={'version': '1.0'})
        rootfiles = etree.SubElement(root, 'rootfiles')
        etree.SubElement(rootfiles, 'rootfile',
                         attrib={'full-path': f'{self.CONST.EPUB}/{package_opf.full_path}',
                                 'media-type': 'application/oebps-package+xml'})
        return SimpleNamespace(**{
            'full_path': '../META-INF/container.xml',
            'read': self.etree_to_string(root),
        })

    async def retrieve_json(self, session, url: str) -> SimpleNamespace:
        result = await self._request(session, url)

        if isinstance(result.json, dict) and \
                set(result.json.keys()) == {'count', 'next', 'previous', 'results'}:
            if not result.json['next']:
                return result.json['results']
            return result.json['results'] + \
                (await self.retrieve_json(session, result.json['next']))

        return result.json

    async def retrieve_book(self) -> SimpleNamespace:
        book = SimpleNamespace(book_id=self.args.book_id)

        async with aiohttp.ClientSession(
                    cookies=self.retrieve_firefox_cookies()
                ) as session:

            logging.info('Checking login')
            await self.check_login(session)

            logging.info('Loading book info')
            book.info = await self.retrieve_json(
                session,
                self.CONST.API_ENDPOINT.format(oreilly=self.args.oreilly,
                                               book_id=book.book_id))

            logging.info('Loading book components')
            components = ['chapters', 'spine', 'files', 'table_of_contents']
            downloaded = await asyncio.gather(*[
                asyncio.create_task(self.retrieve_json(session,
                                                       book.info[component]))
                for component in components
            ])

            for k, v in dict(zip(components, downloaded)).items():
                if self.args.logging_level in ['DEBUG']:
                    logging.debug('Saving components to `debug` directory')
                    await self._write(
                        f'{self.root}/debug/__{k}__.json',
                        json.dumps(v, indent=2).encode(self.CONST.ENCODING)
                    )
                book.__dict__[k] = v

            stylesheets = {
                file['full_path']
                for file in book.files
                for chapter in book.chapters
                if file['url'] in chapter['related_assets']['stylesheets']
            }
            logging.info(f'The following stylesheets are declared: {stylesheets}')

            logging.info('Loading book assets')
            book.assets = await asyncio.gather(*[
                asyncio.create_task(self._request(session, asset['url'], data=asset))
                for asset in book.files
            ])

        return book

    async def run(self):
        logging.info('Loading book')
        book = await self.retrieve_book()

        logging.info('Patching book assets')
        book.assets += [
            item for item in await asyncio.gather(*[
                asyncio.create_task(self._patch(book, asset)) for asset in book.assets
            ]) if item
        ]
        book.assets.append(self.generate_epub_container(book))
        book.assets.append(self.generate_epub_mimetype())

        logging.info('Saving assets')
        await asyncio.gather(*[
            asyncio.create_task(
                self._write(f'{self.root}/{self.CONST.EPUB}/{asset.full_path}', asset.read)
            ) for asset in book.assets if 'inactive' not in asset.__dict__
        ])
        print(f'==========\n\n'
              f'All done; now you can run the following command to generate your EPUB:\n'
              f'\tcd {self.root}; zip -9X ~/Documents/{self.args.book_id}.epub mimetype '
              f'$(find META-INF {self.CONST.EPUB} -type f)\n\n'
              f'Also please run EPUB validator against the resulting file:\n'
              f'\tjava -jar epubcheck.jar ~/Documents/{self.args.book_id}.epub\n\n'
              f'There should be no errors or warnings.')


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--oreilly', default='oreilly.com',
                        help='DO NOT CHANGE; Use this domain name (avoid hardcoding)')
    parser.add_argument('--cookie-file', default='cookies.sqlite',
                        help='Firefox cookie database name')
    parser.add_argument('--email', required=True,
                        help='Email (it is used for login validation only)')
    parser.add_argument('--css-map', default='',
                        help='Replace CSS files with the provided ones; '
                             'format is `full_path1:user_css1[,full_path2:user_css2[...]]`')
    parser.add_argument('--woff2', action='store_true',
                        help='Convert fonts to WOFF2 with `woff2_compress`;'
                             ' if enabled it MUST succeed for all fonts')
    parser.add_argument('--pretty-print', action='store_true',
                        help='Use `pretty_print` LXML option')

    parser.add_argument('-o', '--output', default='eBooks',
                        help='Output directory')
    parser.add_argument('-i', '--book-id', required=True,
                        help='Book ID')
    parser.add_argument('--logging-level', default='INFO',
                        choices=[
                            'NOTSET', 'DEBUG', 'INFO', 'ERROR', 'CRITICAL', 'FATAL', 'WARNING'
                        ],
                        help='Logging level')

    asyncio.run(OreillyBooksOnline(parser.parse_args()).run())
