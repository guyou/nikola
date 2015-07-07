# -*- coding: utf-8 -*-

# Copyright © 2012-2015 Roberto Alsina and others.

# Permission is hereby granted, free of charge, to any
# person obtaining a copy of this software and associated
# documentation files (the "Software"), to deal in the
# Software without restriction, including without limitation
# the rights to use, copy, modify, merge, publish,
# distribute, sublicense, and/or sell copies of the
# Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice
# shall be included in all copies or substantial portions of
# the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY
# KIND, EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE
# WARRANTIES OF MERCHANTABILITY, FITNESS FOR A PARTICULAR
# PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS
# OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR
# OTHER LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR
# OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION WITH THE
# SOFTWARE OR THE USE OR OTHER DEALINGS IN THE SOFTWARE.

from __future__ import unicode_literals, print_function
import os
import re
import sys
import datetime
import io
import json
import requests
from lxml import etree
from collections import defaultdict

try:
    from urlparse import urlparse
    from urllib import unquote
except ImportError:
    from urllib.parse import urlparse, unquote  # NOQA

try:
    import phpserialize
except ImportError:
    phpserialize = None  # NOQA

from nikola.plugin_categories import Command
from nikola import utils
from nikola.utils import req_missing
from nikola.plugins.basic_import import ImportMixin, links
from nikola.nikola import DEFAULT_TRANSLATIONS_PATTERN
from nikola.plugins.command.init import SAMPLE_CONF, prepare_config, format_default_translations_config

LOGGER = utils.get_logger('import_wordpress', utils.STDERR_HANDLER)


class CommandImportWordpress(Command, ImportMixin):
    """Import a WordPress dump."""

    name = "import_wordpress"
    needs_config = False
    doc_usage = "[options] wordpress_export_file"
    doc_purpose = "import a WordPress dump"
    cmd_options = ImportMixin.cmd_options + [
        {
            'name': 'exclude_drafts',
            'long': 'no-drafts',
            'short': 'd',
            'default': False,
            'type': bool,
            'help': "Don't import drafts",
        },
        {
            'name': 'exclude_privates',
            'long': 'exclude-privates',
            'default': False,
            'type': bool,
            'help': "Don't import private posts",
        },
        {
            'name': 'include_empty_items',
            'long': 'include-empty-items',
            'default': False,
            'type': bool,
            'help': "Include empty posts and pages",
        },
        {
            'name': 'squash_newlines',
            'long': 'squash-newlines',
            'default': False,
            'type': bool,
            'help': "Shorten multiple newlines in a row to only two newlines",
        },
        {
            'name': 'no_downloads',
            'long': 'no-downloads',
            'default': False,
            'type': bool,
            'help': "Do not try to download files for the import",
        },
        {
            'name': 'download_auth',
            'long': 'download-auth',
            'default': None,
            'type': str,
            'help': "Specify username and password for HTTP authentication (separated by ':')",
        },
        {
            'name': 'separate_qtranslate_content',
            'long': 'qtranslate',
            'default': False,
            'type': bool,
            'help': "Look for translations generated by qtranslate plugin",
            # WARNING: won't recover translated titles that actually
            # don't seem to be part of the wordpress XML export at the
            # time of writing :(
        },
        {
            'name': 'translations_pattern',
            'long': 'translations_pattern',
            'default': None,
            'type': str,
            'help': "The pattern for translation files names",
        },
    ]
    all_tags = set([])

    def _read_options(self, options, args):
        options['filename'] = args.pop(0)

        if args and ('output_folder' not in args or
                     options['output_folder'] == 'new_site'):
            options['output_folder'] = args.pop(0)

        if args:
            LOGGER.warn('You specified additional arguments ({0}). Please consider '
                        'putting these arguments before the filename if you '
                        'are running into problems.'.format(args))

        self.import_into_existing_site = False
        self.url_map = {}
        self.timezone = None

        self.wordpress_export_file = options['filename']
        self.squash_newlines = options.get('squash_newlines', False)
        self.output_folder = options.get('output_folder', 'new_site')

        self.exclude_drafts = options.get('exclude_drafts', False)
        self.exclude_privates = options.get('exclude_privates', False)
        self.no_downloads = options.get('no_downloads', False)
        self.import_empty_items = options.get('include_empty_items', False)

        self.auth = None
        if options.get('download_auth') is not None:
            username_password = options.get('download_auth')
            self.auth = tuple(username_password.split(':', 1))
            if len(self.auth) < 2:
                print("Please specify HTTP authentication credentials in the form username:password.")
                return False

        self.separate_qtranslate_content = options.get('separate_qtranslate_content')
        self.translations_pattern = options.get('translations_pattern')
        return True

    def _prepare(self, channel):
        self.context = self.populate_context(channel)
        self.base_dir = urlparse(self.context['BASE_URL']).path

    def _adjust_config_template(self, channel, rendered_template):
        rendered_template = re.sub('# REDIRECTIONS = ', 'REDIRECTIONS = ',
                                   rendered_template)

        if self.timezone:
            rendered_template = re.sub('# TIMEZONE = \'UTC\'',
                                       'TIMEZONE = \'' + self.timezone + '\'',
                                       rendered_template)
        return rendered_template

    def _execute(self, options={}, args=[]):
        """Import a WordPress blog from an export file into a Nikola site."""
        if not args:
            print(self.help())
            return False

        if not self._read_options(options, args):
            return False

        # A place holder where extra language (if detected) will be stored
        self.extra_languages = set()

        if not self.no_downloads:
            def show_info_about_mising_module(modulename):
                LOGGER.error(
                    'To use the "{commandname}" command, you have to install '
                    'the "{package}" package or supply the "--no-downloads" '
                    'option.'.format(
                        commandname=self.name,
                        package=modulename)
                )

            if phpserialize is None:
                req_missing(['phpserialize'], 'import WordPress dumps without --no-downloads')

        channel = self.get_channel_from_file(self.wordpress_export_file)
        self._prepare(channel)
        conf_template = self.generate_base_site()

        # If user  has specified a custom pattern for translation files we
        # need to fix the config
        if self.translations_pattern:
            self.context['TRANSLATIONS_PATTERN'] = self.translations_pattern

        self.import_posts(channel)

        self.context['TRANSLATIONS'] = format_default_translations_config(
            self.extra_languages)
        self.context['REDIRECTIONS'] = self.configure_redirections(
            self.url_map)

        # Add tag redirects
        for tag in self.all_tags:
            try:
                tag_str = tag.decode('utf8')
            except AttributeError:
                tag_str = tag
            tag = utils.slugify(tag_str)
            src_url = '{}tag/{}'.format(self.context['SITE_URL'], tag)
            dst_url = self.site.link('tag', tag)
            if src_url != dst_url:
                self.url_map[src_url] = dst_url

        self.write_urlmap_csv(
            os.path.join(self.output_folder, 'url_map.csv'), self.url_map)
        rendered_template = conf_template.render(**prepare_config(self.context))
        rendered_template = self._adjust_config_template(channel, rendered_template)
        self.write_configuration(self.get_configuration_output_path(),
                                 rendered_template)

    @classmethod
    def read_xml_file(cls, filename):
        xml = []

        with open(filename, 'rb') as fd:
            for line in fd:
                # These explode etree and are useless
                if b'<atom:link rel=' in line:
                    continue
                xml.append(line)
        return b'\n'.join(xml)

    @classmethod
    def get_channel_from_file(cls, filename):
        tree = etree.fromstring(cls.read_xml_file(filename))
        channel = tree.find('channel')
        return channel

    @staticmethod
    def populate_context(channel):
        wordpress_namespace = channel.nsmap['wp']

        context = SAMPLE_CONF.copy()
        context['DEFAULT_LANG'] = get_text_tag(channel, 'language', 'en')[:2]
        context['TRANSLATIONS_PATTERN'] = DEFAULT_TRANSLATIONS_PATTERN
        context['BLOG_TITLE'] = get_text_tag(channel, 'title',
                                             'PUT TITLE HERE')
        context['BLOG_DESCRIPTION'] = get_text_tag(
            channel, 'description', 'PUT DESCRIPTION HERE')
        context['BASE_URL'] = get_text_tag(channel, 'link', '#')
        if not context['BASE_URL']:
            base_site_url = channel.find('{{{0}}}author'.format(wordpress_namespace))
            context['BASE_URL'] = get_text_tag(base_site_url,
                                               None,
                                               "http://foo.com/")
        if not context['BASE_URL'].endswith('/'):
            context['BASE_URL'] += '/'
        context['SITE_URL'] = context['BASE_URL']

        author = channel.find('{{{0}}}author'.format(wordpress_namespace))
        context['BLOG_EMAIL'] = get_text_tag(
            author,
            '{{{0}}}author_email'.format(wordpress_namespace),
            "joe@example.com")
        context['BLOG_AUTHOR'] = get_text_tag(
            author,
            '{{{0}}}author_display_name'.format(wordpress_namespace),
            "Joe Example")
        context['POSTS'] = '''(
            ("posts/*.rst", "posts", "post.tmpl"),
            ("posts/*.txt", "posts", "post.tmpl"),
            ("posts/*.md", "posts", "post.tmpl"),
        )'''
        context['PAGES'] = '''(
            ("stories/*.rst", "stories", "story.tmpl"),
            ("stories/*.txt", "stories", "story.tmpl"),
            ("stories/*.md", "stories", "story.tmpl"),
        )'''
        context['COMPILERS'] = '''{
        "rest": ('.txt', '.rst'),
        "markdown": ('.md', '.mdown', '.markdown'),
        "html": ('.html', '.htm')
        }
        '''

        return context

    def download_url_content_to_file(self, url, dst_path):
        if self.no_downloads:
            return

        try:
            request = requests.get(url, auth=self.auth)
            if request.status_code >= 400:
                LOGGER.warn("Downloading {0} to {1} failed with HTTP status code {2}".format(url, dst_path, request.status_code))
                return
            with open(dst_path, 'wb+') as fd:
                fd.write(request.content)
        except requests.exceptions.ConnectionError as err:
            LOGGER.warn("Downloading {0} to {1} failed: {2}".format(url, dst_path, err))

    def import_attachment(self, item, wordpress_namespace):
        url = get_text_tag(
            item, '{{{0}}}attachment_url'.format(wordpress_namespace), 'foo')
        link = get_text_tag(item, '{{{0}}}link'.format(wordpress_namespace),
                            'foo')
        path = urlparse(url).path
        dst_path = os.path.join(*([self.output_folder, 'files'] + list(path.split('/'))))
        dst_dir = os.path.dirname(dst_path)
        utils.makedirs(dst_dir)
        LOGGER.info("Downloading {0} => {1}".format(url, dst_path))
        self.download_url_content_to_file(url, dst_path)
        dst_url = '/'.join(dst_path.split(os.sep)[2:])
        links[link] = '/' + dst_url
        links[url] = '/' + dst_url

        return [path] + self.download_additional_image_sizes(
            item,
            wordpress_namespace,
            os.path.dirname(url)
        )

    def download_additional_image_sizes(self, item, wordpress_namespace, source_path):
        if phpserialize is None:
            return []

        additional_metadata = item.findall('{{{0}}}postmeta'.format(wordpress_namespace))
        if additional_metadata is None:
            return []

        result = []
        for element in additional_metadata:
            meta_key = element.find('{{{0}}}meta_key'.format(wordpress_namespace))
            if meta_key is not None and meta_key.text == '_wp_attachment_metadata':
                meta_value = element.find('{{{0}}}meta_value'.format(wordpress_namespace))

                if meta_value is None:
                    continue

                # Someone from Wordpress thought it was a good idea
                # serialize PHP objects into that metadata field. Given
                # that the export should give you the power to insert
                # your blogging into another site or system its not.
                # Why don't they just use JSON?
                if sys.version_info[0] == 2:
                    try:
                        metadata = phpserialize.loads(utils.sys_encode(meta_value.text))
                    except ValueError:
                        # local encoding might be wrong sometimes
                        metadata = phpserialize.loads(meta_value.text.encode('utf-8'))
                else:
                    metadata = phpserialize.loads(meta_value.text.encode('utf-8'))
                size_key = b'sizes'
                file_key = b'file'

                if size_key not in metadata:
                    continue

                for filename in [metadata[size_key][size][file_key] for size in metadata[size_key]]:
                    url = '/'.join([source_path, filename.decode('utf-8')])

                    path = urlparse(url).path
                    dst_path = os.path.join(*([self.output_folder, 'files'] + list(path.split('/'))))
                    dst_dir = os.path.dirname(dst_path)
                    utils.makedirs(dst_dir)
                    LOGGER.info("Downloading {0} => {1}".format(url, dst_path))
                    self.download_url_content_to_file(url, dst_path)
                    dst_url = '/'.join(dst_path.split(os.sep)[2:])
                    links[url] = '/' + dst_url
                    result.append(path)
        return result

    code_re1 = re.compile(r'\[code.* lang.*?="(.*?)?".*\](.*?)\[/code\]', re.DOTALL | re.MULTILINE)
    code_re2 = re.compile(r'\[sourcecode.* lang.*?="(.*?)?".*\](.*?)\[/sourcecode\]', re.DOTALL | re.MULTILINE)
    code_re3 = re.compile(r'\[code.*?\](.*?)\[/code\]', re.DOTALL | re.MULTILINE)
    code_re4 = re.compile(r'\[sourcecode.*?\](.*?)\[/sourcecode\]', re.DOTALL | re.MULTILINE)

    def transform_code(self, content):
        # http://en.support.wordpress.com/code/posting-source-code/. There are
        # a ton of things not supported here. We only do a basic [code
        # lang="x"] -> ```x translation, and remove quoted html entities (<,
        # >, &, and ").
        def replacement(m, c=content):
            if len(m.groups()) == 1:
                language = ''
                code = m.group(0)
            else:
                language = m.group(1) or ''
                code = m.group(2)
            code = code.replace('&amp;', '&')
            code = code.replace('&gt;', '>')
            code = code.replace('&lt;', '<')
            code = code.replace('&quot;', '"')
            return '```{language}\n{code}\n```'.format(language=language, code=code)

        content = self.code_re1.sub(replacement, content)
        content = self.code_re2.sub(replacement, content)
        content = self.code_re3.sub(replacement, content)
        content = self.code_re4.sub(replacement, content)
        return content

    @staticmethod
    def transform_caption(content):
        new_caption = re.sub(r'\[/caption\]', '', content)
        new_caption = re.sub(r'\[caption.*\]', '', new_caption)

        return new_caption

    def transform_multiple_newlines(self, content):
        """Replaces multiple newlines with only two."""
        if self.squash_newlines:
            return re.sub(r'\n{3,}', r'\n\n', content)
        else:
            return content

    def transform_content(self, content, post_format):
        if post_format == 'wp':
            content = self.transform_code(content)
            content = self.transform_caption(content)
            content = self.transform_multiple_newlines(content)
            return content, 'md'
        elif post_format == 'markdown':
            return content, 'md'
        elif post_format == 'none':
            return content, 'html'
        else:
            return None

    def _create_metadata(self, status, excerpt, tags, categories):
        other_meta = {'wp-status': status}
        if excerpt is not None:
            other_meta['excerpt'] = excerpt
        return tags + categories, other_meta

    def import_item(self, item, wordpress_namespace, out_folder=None):
        """Takes an item from the feed and creates a post file."""
        if out_folder is None:
            out_folder = 'posts'

        title = get_text_tag(item, 'title', 'NO TITLE')
        # link is something like http://foo.com/2012/09/01/hello-world/
        # So, take the path, utils.slugify it, and that's our slug
        link = get_text_tag(item, 'link', None)
        parsed = urlparse(link)
        path = unquote(parsed.path.strip('/'))

        try:
            path = path.decode('utf8')
        except AttributeError:
            pass

        # Cut out the base directory.
        if path.startswith(self.base_dir.strip('/')):
            path = path.replace(self.base_dir.strip('/'), '', 1)

        pathlist = path.split('/')
        if parsed.query:  # if there are no nice URLs and query strings are used
            out_folder = os.path.join(*([out_folder] + pathlist))
            slug = get_text_tag(
                item, '{{{0}}}post_name'.format(wordpress_namespace), None)
            if not slug:  # it *may* happen
                slug = get_text_tag(
                    item, '{{{0}}}post_id'.format(wordpress_namespace), None)
            if not slug:  # should never happen
                LOGGER.error("Error converting post:", title)
                return False
        else:
            if len(pathlist) > 1:
                out_folder = os.path.join(*([out_folder] + pathlist[:-1]))
            slug = utils.slugify(pathlist[-1])

        description = get_text_tag(item, 'description', '')
        post_date = get_text_tag(
            item, '{{{0}}}post_date'.format(wordpress_namespace), None)
        try:
            dt = utils.to_datetime(post_date)
        except ValueError:
            dt = datetime.datetime(1970, 1, 1, 0, 0, 0)
            LOGGER.error('Malformed date "{0}" in "{1}" [{2}], assuming 1970-01-01 00:00:00 instead.'.format(post_date, title, slug))
            post_date = dt.strftime('%Y-%m-%d %H:%M:%S')

        if dt.tzinfo and self.timezone is None:
            self.timezone = utils.get_tzname(dt)
        status = get_text_tag(
            item, '{{{0}}}status'.format(wordpress_namespace), 'publish')
        content = get_text_tag(
            item, '{http://purl.org/rss/1.0/modules/content/}encoded', '')
        excerpt = get_text_tag(
            item, '{http://wordpress.org/export/1.2/excerpt/}encoded', None)

        if excerpt is not None:
            if len(excerpt) == 0:
                excerpt = None

        tags = []
        categories = []
        if status == 'trash':
            LOGGER.warn('Trashed post "{0}" will not be imported.'.format(title))
            return False
        elif status == 'private':
            tags.append('private')
            is_draft = False
            is_private = True
        elif status != 'publish':
            tags.append('draft')
            is_draft = True
            is_private = False
        else:
            is_draft = False
            is_private = False

        for tag in item.findall('category'):
            text = tag.text
            type = 'category'
            if 'domain' in tag.attrib:
                type = tag.attrib['domain']
            if text == 'Uncategorized' and type == 'category':
                continue
            self.all_tags.add(text)
            if type == 'category':
                categories.append(type)
            else:
                tags.append(text)

        if '$latex' in content:
            tags.append('mathjax')

        # Find post format if it's there
        post_format = 'wp'
        format_tag = [x for x in item.findall('*//{%s}meta_key' % wordpress_namespace) if x.text == '_tc_post_format']
        if format_tag:
            post_format = format_tag[0].getparent().find('{%s}meta_value' % wordpress_namespace).text
            if post_format == 'wpautop':
                post_format = 'wp'

        if is_draft and self.exclude_drafts:
            LOGGER.notice('Draft "{0}" will not be imported.'.format(title))
            return False
        elif is_private and self.exclude_privates:
            LOGGER.notice('Private post "{0}" will not be imported.'.format(title))
            return False
        elif content.strip() or self.import_empty_items:
            # If no content is found, no files are written.
            self.url_map[link] = (self.context['SITE_URL'] +
                                  out_folder.rstrip('/') + '/' + slug +
                                  '.html').replace(os.sep, '/')
            if hasattr(self, "separate_qtranslate_content") \
               and self.separate_qtranslate_content:
                content_translations = separate_qtranslate_content(content)
            else:
                content_translations = {"": content}
            default_language = self.context["DEFAULT_LANG"]
            for lang, content in content_translations.items():
                try:
                    content, extension = self.transform_content(content, post_format)
                except:
                    LOGGER.error('Cannot interpret post "{0}" (language {1}) with post ' +
                                 'format {2}!'.format(os.path.join(out_folder, slug), lang, post_format))
                    return False
                if lang:
                    out_meta_filename = slug + '.meta'
                    if lang == default_language:
                        out_content_filename = slug + '.' + extension
                    else:
                        out_content_filename \
                            = utils.get_translation_candidate(self.context,
                                                              slug + "." + extension, lang)
                        self.extra_languages.add(lang)
                    meta_slug = slug
                else:
                    out_meta_filename = slug + '.meta'
                    out_content_filename = slug + '.' + extension
                    meta_slug = slug
                tags, other_meta = self._create_metadata(status, excerpt, tags, categories)
                self.write_metadata(os.path.join(self.output_folder, out_folder,
                                                 out_meta_filename),
                                    title, meta_slug, post_date, description, tags, **other_meta)
                self.write_content(
                    os.path.join(self.output_folder,
                                 out_folder, out_content_filename),
                    content)
            return (out_folder, slug)
        else:
            LOGGER.warn('Not going to import "{0}" because it seems to contain'
                        ' no content.'.format(title))
            return False

    def process_item(self, item):
        # The namespace usually is something like:
        # http://wordpress.org/export/1.2/
        wordpress_namespace = item.nsmap['wp']
        post_type = get_text_tag(
            item, '{{{0}}}post_type'.format(wordpress_namespace), 'post')
        post_id = int(get_text_tag(
            item, '{{{0}}}post_id'.format(wordpress_namespace), "0"))
        parent_id = get_text_tag(
            item, '{{{0}}}post_parent'.format(wordpress_namespace), None)

        if post_type == 'attachment':
            files = self.import_attachment(item, wordpress_namespace)
            # If parent was found, store relation with imported files
            if parent_id is not None:
                self.attachments[int(parent_id)][post_id] = files
            else:
                LOGGER.warn("Attachment #{0} ({1}) has no parent!".format(post_id, files))
        else:
            if post_type == 'post':
                out_folder_slug = self.import_item(item, wordpress_namespace, 'posts')
            else:
                post_type = 'page'
                out_folder_slug = self.import_item(item, wordpress_namespace, 'stories')
            # If post was exported, store data
            if out_folder_slug:
                self.posts_pages[post_id] = (post_type, out_folder_slug[0], out_folder_slug[1])

    def write_attachments_info(self, path, attachments):
        with io.open(path, "wb") as file:
            file.write(json.dumps(attachments).encode('utf-8'))

    def import_posts(self, channel):
        self.posts_pages = {}
        self.attachments = defaultdict(dict)
        for item in channel.findall('item'):
            self.process_item(item)
        # Assign attachments to posts
        for post_id in self.attachments:
            if post_id in self.posts_pages:
                destination = os.path.join(self.output_folder, self.posts_pages[post_id][1],
                                           self.posts_pages[post_id][2] + ".attachments.json")
                self.write_attachments_info(destination, self.attachments[post_id])
            else:
                LOGGER.warn("Found attachments for post or page #{0}, but didn't find post or page. " +
                            "(Attachments: {1})".format(post_id, [e[0] for _, e in self.attachments[post_id].items()]))


def get_text_tag(tag, name, default):
    if tag is None:
        return default
    t = tag.find(name)
    if t is not None and t.text is not None:
        return t.text
    else:
        return default


def separate_qtranslate_content(text):
    """Parse the content of a wordpress post or page and separate
    the various language specific contents when they are delimited
    with qtranslate tags: <!--:LL-->blabla<!--:-->"""
    # TODO: uniformize qtranslate tags <!--/en--> => <!--:-->
    qt_start = "<!--:"
    qt_end = "-->"
    qt_end_with_lang_len = 5
    qt_chunks = text.split(qt_start)
    content_by_lang = {}
    common_txt_list = []
    for c in qt_chunks:
        if not c.strip():
            continue
        if c.startswith(qt_end):
            # just after the end of a language specific section, there may
            # be some piece of common text or tags, or just nothing
            lang = ""  # default language
            c = c.lstrip(qt_end)
            if not c:
                continue
        elif c[2:].startswith(qt_end):
            # a language specific section (with language code at the begining)
            lang = c[:2]
            c = c[qt_end_with_lang_len:]
        else:
            # nowhere specific (maybe there is no language section in the
            # currently parsed content)
            lang = ""  # default language
        if not lang:
            common_txt_list.append(c)
            for l in content_by_lang.keys():
                content_by_lang[l].append(c)
        else:
            content_by_lang[lang] = content_by_lang.get(lang, common_txt_list) + [c]
    # in case there was no language specific section, just add the text
    if common_txt_list and not content_by_lang:
        content_by_lang[""] = common_txt_list
    # Format back the list to simple text
    for l in content_by_lang.keys():
        content_by_lang[l] = " ".join(content_by_lang[l])
    return content_by_lang
