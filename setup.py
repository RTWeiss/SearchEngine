from setuptools import setup

setup(
    name='SearchEngine',
    version='0.1.0',
    packages=['your-app-package'],
    include_package_data=True,
    install_requires=[
        'flask==2.1.2',
        'bs4==0.0.1',
        'requests==2.26.0',
        'threading==4.5.3',
        'pickle-mixin==1.0.2',
        'werkzeug==2.0.2',
    ],
)
