#!/usr/bin/python

# Copyright (c) 2009, tim at brool.com
# Licensed under GPL version 3 -- see http://www.gnu.org/copyleft/gpl.txt for information

# todo: add page support?
# todo: fix slugify (make it match Wordpress's algorithm exactly)

"""wp -- easy maintenance of WordPress through git."""

import sys
import os
import os.path
import getopt
import re
import xmlrpclib
import md5
import functools
import subprocess

class BlogXMLRPC:
    """BlogXMLRPC.  Wrapper for the XML/RPC calls to the blog."""
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)
        self.xrpc = xmlrpclib.ServerProxy(self.url)
        self.get_recent = functools.partial(self.xrpc.metaWeblog.getRecentPosts, 1, self.user, self.password, 5)
        self.new_post = functools.partial(self.xrpc.metaWeblog.newPost, 1, self.user, self.password)
    def get_all(self):
        for post in self.xrpc.metaWeblog.getRecentPosts(1, self.user, self.password, 5):
            yield post
        for post in self.xrpc.metaWeblog.getRecentPosts(1, self.user, self.password)[5:]:
            yield post
    def get_post(self, post_id): return self.xrpc.metaWeblog.getPost(post_id, self.user, self.password)
    def edit_post(self, post_id, post): return self.xrpc.metaWeblog.editPost(post_id, self.user, self.password, post, True)

class Git:
    def __init__(self, **kwargs):
        self.repo = self.work = None
        self.keys = set()
        self.taglist = []
        self.__dict__.update(kwargs)

    def git(self, *args):
        args = tuple(['git'] + list(args))
        environ = os.environ.copy()
        if self.repo: environ['GIT_DIR'] = self.repo
        if self.work: environ['GIT_WORK_TREE'] = self.work
        proc = subprocess.Popen(args, stdout=subprocess.PIPE, env=environ)
        return proc.communicate()[0]

    def uncommitted(self):
        return self.git('ls-files', '-m', '--full-name').split('\n')

    def config(self, name):
        try:
            return self.git('config', '--get', name).strip()
        except Exception, e:
            return None

    def has(self, name):
        self.keys = self.keys or set(self.git('ls-files', '--full-name').split('\n'))
        return name in self.keys

    def tags(self):
        self.taglist = self.taglist or self.git('tag', '-l').split('\n')
        return self.taglist

    def version(self, name, commit=None):
        return self.git('show', '%s:%s' % (commit or 'HEAD', name))

    def diff(self, base, current):
        output = self.git('diff', '--numstat', "%s..%s" % (base, current))
        return [ x.split()[2] for x in output.split('\n') if x ]

    def add(self, fname):
        self.git('add', fname)

class Post:
    """Post.  A set of key => value pairs."""
    ignore_fields = set([ 'custom_fields', 'sticky' ])
    read_only_fields = set([ 'dateCreated', 'date_created_gmt' ])

    def __init__(self, keys=None):
        self.post = keys and dict(keys) or None

    def __str__(self):
        buffer = []
        lst = self.post.keys()
        lst.sort()
        lst.remove('description')
        for key in lst:
            if key not in Post.ignore_fields:
                buffer.append ( ".%s %s" % (key, self.post[key]) )
        buffer.append(self.post['description']) 
        return '\n'.join(map(lambda x: x, buffer))

    def parse(self, contents):
        self.post = {}

        dots = True
        description = []
        for line in contents.split('\n'):
            if dots and line and line[0] == '.':
                pos = line.find(' ')
                if pos != -1:
                    key = line[1:pos]
                    if key not in Post.ignore_fields:
                        self.post[key] = line[pos+1:]
                else:
                    if key not in Post.ignore_fields:
                        self.post[line[1:]] = ''
            else:
                description.append(line)
                dots = False

        self.post['description'] = '\n'.join(description)
        return self

    def as_dict(self):
        d = dict(self.post)
        for key in Post.read_only_fields: 
            if key in d: del d[key]
        return d

    # Somewhat close to formatting.php/sanitize_title_with_dashes but without the accent handling
    @staticmethod
    def slugify(inStr):
        slug = re.sub(r'<.*?>', '', inStr)
        slug = re.sub(r'(%[a-fA-F0-9]{2})', '---\1---', slug)
        slug = re.sub(r'%', '', slug)
        slug = re.sub(r'---(%[a-fA-F0-9]{2})---', '\1', slug)
        # should remove accents here
        slug = slug.lower()
        slug = re.sub(r'&.+?;', '', slug)
        slug = re.sub(r'[^%a-z0-9 _-]', '', slug)
        slug = re.sub(r'\s+', '-', slug)
        slug = re.sub(r'-+', '-', slug)
        slug = slug.strip('-')

        return slug

    def filename(self):
        fname = self.post.get('wp_slug') or Post.slugify(self.post['title']) or str(self.post['postid'])
        created = str(self.post['dateCreated'])
        if self.post.get('post_status', 'draft') == 'draft':
            return os.path.join('draft', fname)
        else:
            return os.path.join(created[0:4], created[4:6], fname)

    def id(self):
        return int(self.post.get('postid', 0))

    def signature(self):
        return md5.md5(str(self)).digest()

    def write(self, writeTo=None):
        try:
            (dir, filename) = os.path.split(writeTo or self.filename())
            if not os.path.exists(dir): os.makedirs(dir)
            file(writeTo or self.filename(), 'wt').write(str(self))
        except Exception, e:
            print "wp:", e
            pass

def get_changed_files(basedir, xml, maxUnchanged=5):
    """Compare the local file system with the blog to see what files have changed.
    Check blog entries until finding maxUnchanged unmodified entries."""

    created = []
    changed = []

    unchanged = 0
    for post in xml.get_all():
        xml_post = Post(keys=post)
        fname = os.path.join(basedir, xml_post.filename())
        if os.path.exists(fname):
            local_post = Post().parse(file(fname, 'rt').read())
            if xml_post.signature() != local_post.signature():
                changed.append(xml_post)
            else:
                unchanged += 1
                if unchanged > maxUnchanged: break
        else:
            created.append(xml_post)

    return (created, changed)

def download_files(xml):
    """Download all files for the given blog into the current directory."""

    for post in xml.get_all():
        p = Post(keys=post)
        if not os.path.exists(p.filename()):
            p.write()
        else:
            print "skipping %s, file in way" % p.filename()

def up_until(fn):
    """Walks up the directory tree until a function that takes a path name returns True."""
    curdir = os.getcwd()
    while True:
        if fn(curdir): return curdir
        newdir = os.path.normpath(os.path.join(curdir, os.path.pardir))
        if (newdir == curdir): return False
        curdir = newdir

if __name__ == "__main__":

    if len(sys.argv) == 1 or sys.argv[1] not in ['download', 'status', 'update', 'post']:
        print "usage: wp [command]"
        print "where command is:"
        print "    download -- download everything at once"
        print "    status   -- compare local filesystem vs. blog"
        print "    update   -- bring down changes to local filesystem"
        print "    post     -- post articles to blog"
        print 
        print "Note that you should set up wp.url, wp.user, and wp.password"
        print "using git config"
        sys.exit()

    (options, args) = getopt.getopt(sys.argv[1:], "", [ 'url=', 'user=', 'password=', 'local' ])
    options = dict(options)

    git = Git()
    gitdir = up_until(lambda path: os.path.exists(os.path.join(path, '.git')))

    def check_git_dir():
        if not gitdir: 
            print "Couldn't find .git directory anywhere, aborting."
            sys.exit(1)

    url = git.config('wp.url') or options.get('--url', None)
    user = git.config('wp.user') or options.get('--user', None)
    password = git.config('wp.password') or options.get('--password', None)

    if not url or not user or not password:
        print "wp: need --url, --user, and --password"
        sys.exit(1)

    xml = BlogXMLRPC(url=url, user=user, password=password)

    if args[0] == 'download':
        download_files(xml)
        
    elif args[0] == 'status':
        numToCheck = 5
        if len(args) > 1 and args[1] == 'all':
            numToCheck = sys.maxint
        check_git_dir()
        (created, changed) = get_changed_files(gitdir, xml, maxUnchanged=numToCheck)

        if created: print "\n".join(map(lambda x: "new: %s" % x.filename(), created))
        if changed: print "\n".join(map(lambda x: "changed: %s" % x.filename(), changed))

    elif args[0] == 'update':
        check_git_dir()
        (created, changed) = get_changed_files(gitdir, xml)

        for xml_post in created + changed:
            xml_post.write(os.path.join(gitdir, xml_post.filename()))
            print "updated: %s" % xml_post.filename()

    elif args[0] == 'post':
        check_git_dir()
        for fname in args[1:]:
            try:
                p = Post().parse( file(fname, 'rt').read() )
                if p.id():
                    xml.edit_post( p.id(), p.as_dict() )
                    print "edited: %s" % fname
                else:
                    id = xml.new_post( p.as_dict() )

                    # since it's a new post, get it back again and write it out
                    np = Post(keys = xml.get_post( id ))
                    newname = os.path.join( gitdir, np.filename() )
                    np.write(newname)
                    print "posted: %s -> %s" % (fname, newname)
            except IOError:
                print "Couldn't open %s, continuing." % fname
            except Exception, e:
                print "wp:", e
                sys.exit(1)

                
        
