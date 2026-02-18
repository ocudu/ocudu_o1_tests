#!/usr/bin/python3

import sys
import os
import subprocess
import json
import re
from typing import List

class Installer:

    def __init__(self, url, branch, dstFolder) -> None:
        self.downloadUrl = url
        self.publicUrlFormat = self.createPublicUrlFormat(url, branch)
        print(f'fmt={self.publicUrlFormat}')
        self.branch = branch
        self.baseFolder = dstFolder
        self.subfolder = self.createSubFolder(url, branch)

    def createPublicUrlFormat(self, url:str, branch:str)->str:
        if url.endswith('.git'):
            url = url[:-4]
        if url.startswith('git@'):
            url = 'https://'+url[4:]
        fmt=url+'/raw/'+branch+'/{}'
        return fmt
    def createSubFolder(self, gitUrl:str, branch:str) -> str:
        regex = r"^[^\/]+\/\/(.*)$"
        matches = re.finditer(regex, gitUrl)
        match = next(matches)
        name = match.group(1)
        if name.endswith('.git'):
            name=name[:-4]
        tmp:List[str]=[]
        hlp1 = name.split('/')
        for h in hlp1:
            if '.' in h:
                hlp2=h.split('.')
                for h2 in hlp2:
                    tmp.append(h2)
            else:
                tmp.append(h)

        return '/'.join(tmp)+'/'+branch 
    
    def getDstFolder(self)->str:
        return f'{self.baseFolder}/{self.subfolder}'
    
    def exec(self, cmd:str):
        output = subprocess.Popen(
            cmd, shell=True, stdout=subprocess.PIPE).stdout.read()
        return output

    def download(self) -> bool:
        print(f'try to download repo {self.downloadUrl} to {self.getDstFolder()}')
        self.exec(f'git clone --single-branch --branch {self.branch} {self.downloadUrl} {self.getDstFolder()}')
    
    def getFilesFiltered(self, lst:List[str]=None, path=None, root=None, filter=['yaml','yml'])->List[str]:
        if lst is None:
            lst=[]
        if root is None:
            root=str(self.getDstFolder())
        if path is None:
            path=self.getDstFolder()
        if os.path.exists(path) and os.path.isdir(path):
            # Iterate over all files and directories in the given path
            for filename in os.listdir(path):
                if filename.startswith("."):
                    continue
                fmatch=False
                # Get the absolute path of the file/directory
                abs_path = os.path.join(path, filename)
                # If it is a directory, recursively call this function on it
                if os.path.isdir(abs_path):
                    self.getFilesFiltered(lst=lst, path=abs_path, root=root, filter=filter )
                # If it is a file, print its absolute path
                elif os.path.isfile(abs_path):
                    for fi in filter:
                        if abs_path.endswith(fi):
                            fmatch=True
                            break
                    if not fmatch:
                        continue
                    relpath=abs_path[len(root)+1:]
                    lst.append(relpath)
        return lst

    def urlAlreadyInData(self, data:List[dict], pubUrl:str, key='publicURL'):
        for item in data:
            if key in item and item[key]==pubUrl:
                return True
        return False

    def createSchemaMap(self):
        schemaMapFile = f'{self.baseFolder}/schema-map.json'
        if os.path.isfile(schemaMapFile):
            with open(schemaMapFile) as fp:
                data = json.load(fp)
        else:
            data:List[dict] = []
        files = self.getFilesFiltered()
        for file in files:
            print(file)
            pubUrl = self.publicUrlFormat.format(file)
            if self.urlAlreadyInData(data,pubUrl):
                print(f'entry with url {pubUrl} already exists. ignoring')
                continue
            data.append({
                'publicURL': pubUrl,
                'localURL': f'{self.subfolder}/{file}'
            })
        with open(schemaMapFile,'w') as fp:
            json.dump(data,fp)

def printHelp():
    print("Installation script for VES additional formats")
    print(" usage: ")
    print("    install.py DOWNLOAD-REPO BRANCH DESTINATION-FOLDER")

args = sys.argv
args.pop(0)
if len(args)<3:
    print(f'bad parameters ${args}')
    printHelp()
    exit(1)

dlRepo = args.pop(0)
dlBranch = args.pop(0)
dst = args.pop(0)
installer = Installer(dlRepo, dlBranch, dst)
installer.download()

installer.createSchemaMap()