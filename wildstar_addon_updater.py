#******************************************#
# Kalle's Wildstar Addon Updater           #
# Version 3.0   8/9/2014                   #
#******************************************#

import sys
sys.dont_write_bytecode = True  # Prevents Python from creating __PyCache__ folders

import os
from io import BytesIO  # used for zipfile extraction
from zipfile import ZipFile, BadZipfile
from bs4 import BeautifulSoup, SoupStrainer
import requests
import re
import json
from urllib.parse import urljoin

import tkinter as tk
import tkinter.ttk as ttk
import tkinter.filedialog as filedialog
import tkinter.messagebox as messagebox
import threading
import queue

import pytest

CURSEFORGE_ADDON_PAGE = 'http://wildstar.curseforge.com/ws-addons'
MAIN_ADDON_PAGE = 'http://www.curse.com/ws-addons/wildstar'
APP_TITLE = "Wildstar Addon Updater"

#!BUGS:
# 1. I need another way to compare installed addons to the ones on curseforge
#   - 'TB-Graphics Options' becomes TBGO after downloading
#   - 'The Visitor' becomes 'Visitor'
#   Ideas to fix:
#       - Redownload ALL the addons, but then store the name extracted from
#       the main curseforge addon listing page inside the Config file
#           - does NOT handle case where an addon is manually downloaded, then needs to be updated
#       - Extract the folder as an addon with the same name as the curseforge addon listing
#           - will not recognize currently installed addons and then overwrite them
# 2. If Addons get removed from the main folder, the Config file will continue to store them.
#   - Add a delete config method, then call it in 'get_pending_downloads' whenever an addon is in the
# config file but not in the directory

# 3. GUI revamp:
#   - Route error messages like "addon not found" or "connection timed out" to the text box
# 4. Addon creation date test does not work
# 5. Print out which addons were not updated, which addons could not be updated, at the end
# 6. Make sure threads stop if program is exited! (Current version continues)
# 7. Extract functions like the string helpers into their own class or into a 'Helpers' file
# 8. Use decorators to wrap common functionality like enabling then disabling something in a method.
# 9. Write tests for everything using HTTPretty, Sure, Mock, py.test, and Nose.

def convert_addon_name(name):
    pattern = re.compile(r'[^a-zA-Z0-9]')
    return re.sub(pattern, '', name.strip())

def log(error):
    with open('errors.log', 'a') as error_log:
        error_log.write(str(error))

def http_request(url, url_params=None):
    try:
        response = requests.get(url, params=url_params, timeout=10)
        response.raise_for_status()
        return response
    except requests.exceptions.Timeout:
        print("Connection timed out.")
    except requests.exceptions.RequestException as err:
        log(err)


class MultiQueue(object):

    def __init__(self):
        self._task_queue = queue.Queue()    # FIFO
        self._message_queue = queue.Queue()
        self._warning_queue = queue.Queue()

    def put_task(self, task):
        self._task_queue.put(task)
    def put_message(self, message):
        self._message_queue.put(message)
    def put_warning(self, warning):
        self._warning_queue.put(warning)

    def get_task(self):
        return self._task_queue.get()
    def get_message(self):
        return self._message_queue.get()
    def get_warning(self):
        return self._warning_queue.get()

    def task_available(self):
        return self._task_queue.qsize()
    def message_available(self):
        return self._message_queue.qsize()
    def warning_available(self):
        return self._warning_queue.qsize()


class Config(object):
    CONFIG_FILE = 'config.json'

    def __init__(self, file_name=CONFIG_FILE):
        self._config_file = file_name
        self._config_dict = self.decode()
        
    def decode(self):
        """
        Returns a python dict representing the config file.
        """
        try:
            with open(self._config_file, 'r') as config_file:
                return json.loads(config_file.read())
        except (FileNotFoundError, ValueError, KeyError):
            return self._default()
    
    def encode(self):
        """
        Saves the stored dictionary as a JSON file.
        """
        with open(self._config_file, 'w') as config_file:
            config_file.write(self._formatted_json(self._config_dict))

    def get_addon(self, name):
        try:
            return JsonAddon(self._config_dict['addons'].get(name))
        except ValueError:
            return

    def get_addons(self):
        return { name:JsonAddon(info) for name, info in self._config_dict['addons'].items() }

    def addon_names(self):
        return self._config_dict['addons'].keys()

    def update_addon(self, addon):
        assert addon
        self._config_dict['addons'][addon.get_name()] = addon.to_json()
        self.encode()

    def add_addons(self, addons):
        assert isinstance(addons, dict) or isinstance(addons, list), '{0}'.format(addons)
        if isinstance(addons, dict):
            addons = addons.values()
        for addon in addons:
            self.update_addon(addon)
        self.encode()

    def get_directory(self):
        return self._config_dict['config'].get('PATH', '')

    def update_directory(self, directory):
        assert isinstance(directory, str)
        if directory:
            self._config_dict['config']['PATH'] = directory
            self.encode()
                
    def _formatted_json(self, json_dict):
        assert isinstance(json_dict, dict)
        return json.dumps(json_dict, sort_keys=True, indent=4)

    def _default(self):
        return {'config':{}, 'addons':{}}


class Addon(object):
    ADDON_DL_SUFFIX = '/files/latest'
    
    def __init__(self, name, url, epoch_date):
        if not all([name, url, epoch_date]):
            raise ValueError("invalid arguments passed to Addon constructor")
        self._name = convert_addon_name(name)
        assert re.match(r'[a-zA-Z0-9]+', self._name), "invalid name format: {0}".format(self._name)
        self._url = url
        self._full_url = '{0}{1}'.format(url, self.ADDON_DL_SUFFIX)
        self._epoch_date = int(epoch_date)

    def get_name(self):
        return self._name
    def get_url(self):
        return self._url
    def get_full_url(self):
        return self._full_url
    def get_date(self):
        return self._epoch_date
    def to_json(self):
        return {'name':self._name, 'url':self._url, 'date':self._epoch_date}
    def __str__(self):
        return '{0} => ({1}, {2})'.format(self._name, self._url, self._epoch_date)
    def __repr__(self):
        return 'Addon({0}, {1}, {2})'.format(self._name, self._url, self._epoch_date)


class JsonAddon(Addon):

    def __init__(self, json_dict):
        if not isinstance(json_dict, dict):
            raise ValueError("'json_dict' must be a valid python dictionary not '{0}'".format(json_dict))
        Addon.__init__(self, json_dict['name'], json_dict['url'], json_dict['date'])


class Message(object):
    """
    Message objects store information that can be passed between classes.
    The information is retrieved using Python dictionary syntax with strings as the keys.
    """
    def __init__(self, **kwargs):
        self._messages = {}
        for key, value in kwargs.items():
            self._messages[key] = value

    def get(self, key):
        return self._messages.get(key)
    def __str__(self):
        return str(self._messages)
    def __repr__(self):
        return '{0}(self._messages)'.format('Message', self._messages)


class AddonSearch(threading.Thread):

    BASE_CURSE_URL = 'http://www.curse.com/search/ws-addons'
    BASE_URL_CURSEFORGE = 'http://wildstar.curseforge.com/'
    SEARCH_START = "Searching for addon updates..."

    def __init__(self, addons, queue):
        threading.Thread.__init__(self)
        self._addons = addons
        self.queue = queue
        self._no_results_found = {}
        self._retrieved_addons = {}

    def run(self):
        self.queue.put_message(Message(msg=self.SEARCH_START))
        for addon in self._addons:
            msg = Message(msg="Searching for '{0}'...".format(addon))
            self.queue.put_message(msg)
            online_addon = self.find(addon)
            if online_addon:
                task = Message(current_addon=addon, online_addon=online_addon)
                self.queue.put_task(task)

    def find(self, addon_name):
        """
        Attempts to find an addon from an online source if it does not exist
        in the _retrieved_addons hash table already.
        :param addon_name: a string
        :return: an Addon or None
        """
        if self._no_results_found.get(addon_name):
            return
        addon = self._retrieved_addons.get(addon_name)
        return addon if addon else self._search_online(addon_name)

    def _search_online(self, addon_name):
        """
        Searches Curse.com for the given addon name then returns
        it as an Addon if found, otherwise returns None.
        """
        assert isinstance(addon_name, str), "The addon name must be a string."
        page = self._search_request(addon_name)
        if page:
            name, url, date = self._find_addon_info_on(page)
            if name and url and date:
                addon = Addon(name, url, date)
                self._retrieved_addons[name] = addon
                return addon
        self._no_results_found_with(addon_name)
        self.queue.put_warning(Message(msg="Unable to find '{0}' on Curse.".format(addon_name)))

    def _search_request(self, addon_name):
        search_terms = {
            addon_name,
            self._split_by_camel_case(addon_name),
            self._remove_last_word(addon_name)
        }
        for search_term in search_terms:
            if search_term:
                url_params = {'search':search_term}
                page = http_request(self.BASE_CURSE_URL, url_params)
                results = self._results_found_on(page)
                return page if results else self._no_results_found_with(addon_name)

    def _no_results_found_with(self, addon_name):
        self._no_results_found[addon_name] = True

    def _split_by_camel_case(self, addon_name):
        try:
            return ' '.join(re.findall(r'[A-Z]{1}[a-z0-9]+', addon_name))
        except TypeError:
            return ''

    # BUG DETECTED: may invalidate a search like this: 'CHouse'
    def _remove_last_word(self, addon_name):
        """
        :param addon_name: a string
        :return: a string
        self._remove_last_word(SpaceStashCore)
        >>> "SpaceStash"
        """
        try:
            return ''.join(re.findall(r'[A-Z]{1}[a-z0-9]+', addon_name)[:-1])
        except TypeError:
            return ''

    def _results_found_on(self, page):
        try:
            no_results_tag = SoupStrainer('li', class_='no-results')
            result = BeautifulSoup(page.text, parse_only=no_results_tag)
            return not 'No results for' in result.text
        except (TypeError, AttributeError):
            return True

    def _find_addon_info_on(self, page):
        """
        Given an html search page, this method finds the first name, url,
        and date from the html tags.  May return a tuple of None values if the search did
        not return any results.
        """
        assert page
        tr_tags = SoupStrainer('tr', class_='wildstar')
        tags = BeautifulSoup(page.text, parse_only=tr_tags)
        first_link = tags.find('a')
        name, url, date = None, None, None
        if first_link:
            name = convert_addon_name(first_link.text)
            url = self._make_full_url_from(first_link.get('href'))
            date = self._find_addon_date(url)
        return (name, url, date)

    def _find_addon_date(self, url):
        """
        Returns an integer such as 10512508 that represents the date in seconds,
        otherwise None.
        """
        page = http_request(url)
        if not page:
            return
        li_tags = SoupStrainer('ul', class_="cf-details")
        tags = BeautifulSoup(page.text, parse_only=li_tags)
        for tag in tags.find_all('li'):
            if 'Last Released File' in tag.text:
                return tag.find('abbr').get('data-epoch')

    def _make_full_url_from(self, partial_url):
        return urljoin(self.BASE_URL_CURSEFORGE, self._remove_page(partial_url))

    def _remove_page(self, url):
        return url.replace('/wildstar/', '/')


class Downloader(threading.Thread):

    def __init__(self, addon_directory, queue):
        threading.Thread.__init__(self)
        self._queue = queue
        self._addon_directory = addon_directory
        self._config = Config()

    def run(self):
        while self._queue.task_available():
            try:
                task = self._queue.get_task()
                current_addon = task.get('current_addon')
                online_addon = task.get('online_addon')

                if current_addon and online_addon:
                    msg = Message(msg='Downloading {0}...'.format(online_addon.get_name()))
                    self._queue.put_message(msg)
                    self._extract_zipfile(online_addon.get_full_url())
                    self._config.update_addon(online_addon)
            except queue.Empty:
                pass

    def _get_pending_downloads(self):
        download_list = []
        for addon_name in os.listdir(self._addon_directory):
            if self._addon_exists(addon_name) and self._update_required(addon_name):
                download_list.append(addon_name)
        return set(download_list)  # Remove duplicates

    def _update_required(self, addon_name):
        assert isinstance(addon_name, str), 'The addon name must be a string not \'{0}\''.format(addon_name)
        installed_addon = self._config.get_addon(addon_name)
        online_addon = self._search.find(addon_name)

        # DEBUG
        if not installed_addon or (installed_addon.get_date() == online_addon.get_date()):
            return True
        if self._directory_creation_date(addon_name) < online_addon.get_date():
            return True
        return False

    def _addon_exists(self, addon_name):
        return os.path.isdir(os.path.join(self._addon_directory, addon_name)) and \
               self._search.find(addon_name)

    def _directory_creation_date(self, directory):
        return os.stat(os.path.join(self._addon_directory, directory)).st_ctime

    def _extract_zipfile(self, url):
        try:
            response = http_request(url)
            with ZipFile(BytesIO(response.content)) as zip_file:
                zip_file.extractall(path=self._addon_directory)
        except (TypeError, BadZipFile) as err:
            log(err)


class DownloaderInterface(tk.Tk):

    def __init__(self, config=Config()):
        tk.Tk.__init__(self)
        self.queue = MultiQueue()
        self._config = config
        self._directory = config.get_directory()
        self._create_and_center_the_main_window()
        self._create_gui()

    def start_thread(self):
        """
        Starts the download and search threads after the button click event occurs.
        """
        if not self._directory:
            return messagebox.showerror("Error", "Select a valid Wildstar addon directory first.")

        self.button.config(state='disabled')

        self._addons = []
        for addon_name in os.listdir(self._directory):
            if os.path.isdir(os.path.join(self._directory, addon_name)):
                self._addons.append(addon_name)

        self.thread1 = AddonSearch(self._addons, self.queue)
        self.thread2 = Downloader(self._directory, self.queue)

        self.progressbar['value'] = 0.0
        self.progressbar['maximum'] = 1.0

        self.thread1.start()
        self.periodic_call()

    def periodic_call(self):
        self._check_download_queue()
        self._check_message_queue()

        if self.thread1.is_alive() or self.thread2.is_alive():
            self.after(100, self.periodic_call)
        elif not(self.thread1.is_alive() and self.thread2.is_alive()):
            self.button.config(state='active')
            self.progressbar['value'] = self.progressbar['maximum']
            self._update_listbox("\nUnable to find updates for the following addons: ")
            while self.queue.warning_available():
                msg = self.queue.get_warning()
                self._update_listbox(msg.get('msg'))

    def _check_download_queue(self):
        if self.queue.task_available() and not self.thread2.is_alive():
           self.thread2 = Downloader(self._directory, self.queue)
           self.thread2.start()

    def _check_message_queue(self):
        while self.queue.message_available():
            try:
                msg = self.queue.get_message()
                self.listbox.insert('end', msg.get('msg'))
                total_downloads = msg.get('total_downloads')
                if total_downloads:
                    self.progressbar.step(1 / total_downloads)
            except Queue.Empty:
                pass

    def get_directory(self):
        self._directory = filedialog.askdirectory(mustexist=True)
        self._config.update_directory(self._directory)

    def _create_and_center_the_main_window(self):
        self.wm_title(APP_TITLE)
        self.resizable(0, 0)

        self.wm_withdraw()
        self.update_idletasks()
        x = (self.winfo_screenwidth() - self.winfo_reqwidth()) // 2
        y = (self.winfo_screenheight() - self.winfo_reqheight()) // 2
        self.wm_geometry('+{0}+{1}'.format(x, y))
        self.wm_deiconify()

    def _create_gui(self):
        self.listbox = tk.Listbox(self)
        self.progressbar = ttk.Progressbar(self, orient='horizontal', length=300, mode='determinate')
        self.button = tk.Button(self, text="Update", command=self.start_thread)
        self.directory_button = tk.Button(self, text="Change Addon Folder", command=self.get_directory)
        self.quit_button = tk.Button(self, text="Quit", command=self.destroy)

        self.listbox.pack(padx=10, pady=10, side=tk.TOP, expand=True, fill=tk.BOTH)
        self.progressbar.pack(padx=10, pady=10, expand=True, fill=tk.BOTH)
        self.button.pack(padx=10, pady=10, side=tk.LEFT, expand=True, fill=tk.BOTH)
        self.directory_button.pack(padx=10, pady=10, side=tk.LEFT, expand=True, fill=tk.BOTH)
        self.quit_button.pack(padx=10, pady=10, side=tk.RIGHT, expand=True, fill=tk.BOTH)

    def _update_listbox(self, message, pos='end'):
        self.listbox.insert(pos, message)

if __name__ == "__main__":
    pytest.main('-s')  # Run tests (the '-s' flag allows print statements to work)
    app = DownloaderInterface()
    app.mainloop()