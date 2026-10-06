from setuptools import setup

from os import path
this_directory = path.abspath(path.dirname(__file__))
with open(path.join(this_directory, 'README.md'), encoding='utf-8') as f:
    long_description = f.read()

setup(name='cbpi4-boilpower',
      version='0.0.2',
      description='CraftBeerPi4 boil kettle logic: full power to the boil, then a duty the brewer sets from the dashboard',
      author='Scott Kivitz',
      url='https://github.com/sbkivitz/cbpi4-boilpower',
      include_package_data=True,
      package_data={
      '': ['*.txt', '*.rst', '*.yaml'],
      'cbpi4-boilpower': ['*', '*.txt', '*.rst', '*.yaml']},
      packages=['cbpi4-boilpower'],
      long_description=long_description,
      long_description_content_type='text/markdown'
     )
