"""Main module."""
import os, pwd, asyncio, time
import re
from datetime import datetime
from async_generator import yield_, asynccontextmanager, async_generator
from typing import List
import json
from datetime import datetime, timedelta
import batchspawner
from jinja2 import Environment, BaseLoader
from slugify import slugify
# pcluster_spawner_template_paths = os.path.join(os.path.dirname(__file__), 'templates')
from typing import Any, List
import requests
from async_generator import async_generator, yield_
import os
import boto3
import pwd
from tornado import gen
from concurrent.futures import ThreadPoolExecutor
from traitlets import Unicode
from aws_pcluster_helpers.models.sinfo import SInfoTable, SinfoRow
from cdsdashboards.hubextension.spawners.variablemixin import VariableMixin, MetaVariableMixin

class PClusterSlurmSpawner(batchspawner.SlurmSpawner):
    #-------------------------------------------[ Project list drop-down menu ]---------------------------------------------
    async def start(self):
        # Get user options from form data
        user_options = self.user_options
        selected_profile = user_options.get('profile')

        if selected_profile and 'profile-option-' + selected_profile + '-project' in user_options:
            # Extract selected project
            selected_project = user_options['profile-option-' + selected_profile + '-project']

            # Save project name
            self.save_project_name(selected_project)
        # Call the original start method
        return await super().start()
    #-------------------------------------------X Project list drop-down menu X---------------------------------------------
    # This is tied to the dict returned by the submission_data function
    batch_script = Unicode(
        """#!/bin/bash
#SBATCH --output={{homedir}}/logs/jupyterhub_%j.log
#SBATCH --job-name=jupyterhub
#SBATCH --chdir={{homedir}}
#SBATCH --export={{keepvars}}
#SBATCH --get-user-env=L
{% if req_partition  %}#SBATCH --partition={{req_partition}}{% endif %}
{% if req_constraint %}#SBATCH --constraint={{req_constraint}}{% endif %}
{% if req_nprocs %}#SBATCH --cpus-per-task={{req_nprocs}}{% endif %}
{% if exclusive %}#SBATCH --exclusive{% endif %}
{% if runtime    %}#SBATCH --time={{runtime}}{% endif %}
{% if options    %}#SBATCH {{options}}{% endif %}
set -euo pipefail
trap 'echo SIGTERM received' TERM

{{prologue}}

# Custom R
{% if req_custom_r    %}export PATH="{{req_custom_r}}:$PATH"{% endif %}

# Custom Env
{% if req_custom_env    %}{{req_custom_env}}{% endif %}

which jupyterhub-singleuser
{% if srun %}{{srun}} {% endif %}{{cmd}}
echo "jupyterhub-singleuser ended gracefully"
{{epilogue}}
    """
    )
    profile_form_template = Unicode(
    """
    <style>
    #pclusterslurmspawner-profiles-list .profile {
        display: flex;
        flex-direction: row;
        font-weight: normal;
        border-bottom: 1px solid #ccc;
        padding-bottom: 12px;
    }
    #pclusterslurmspawner-profiles-list .profile .radio {
        padding: 12px;
    }
    #pclusterslurmspawner-profiles-list .profile .option {
        display: flex;
        flex-direction: row;
        align-items: center;
        padding-bottom: 12px;
    }
    #pclusterslurmspawner-profiles-list .profile .option label {
        font-weight: normal;
        margin-right: 8px;
        min-width: 200px; /* Adjust the min-width value to fit your requirements */
    }
    #pclusterslurmspawner-profiles-list .profile .option select {
        min-width: 150px; /* Adjust the min-width value to fit your requirements */
    }
    /* Additional style to apply monospace font only to certain options */
    #pclusterslurmspawner-profiles-list .profile .option .value {
        white-space: pre;
    }
    /* Apply monospace font to instance type, GB, and price options */
    #pclusterslurmspawner-profiles-list .profile .option select.instance-types,
    #pclusterslurmspawner-profiles-list .profile .option select.gb,
    #pclusterslurmspawner-profiles-list .profile .option select.price {
        font-family: monospace;
        font-size: 16px;
    }
</style>

<div class='form-group' id='pclusterslurmspawner-profiles-list'>
    {% for profile in profile_list %}
    <div class='profile'>
        <div class='radio'>
            <input type='radio' name='profile' id='profile-item-{{ profile.slug }}' value='{{ profile.slug }}' {% if profile.default %}checked{% endif %} required />
        </div>
        <div>
            <h3>{{ profile.display_name }}</h3>
            {%- if profile.description %}
            <p>{{ profile.description }}</p>
            {%- endif %}
            {%- if profile.profile_options %}
            <div class='profile-options'>
                {%- for option_key, option_value in profile.profile_options.items() %}
                <div class='option'>
                    <label for='profile-option-{{ profile.slug }}-{{ option_key }}'>{{ option_value.display_name }}</label>
                    <div class='value'>
                        <select name='profile-option-{{ profile.slug }}-{{ option_key }}' class='form-control {% if option_key == "instance_types" %}instance-types{% elif option_key == "gb" %}gb{% elif option_key == "price" %}price{% endif %}'>
                            {%- for choice_key, choice_value in option_value.choices.items() %}
                            <option value='{{ choice_key }}' {% if choice_value.default %}selected{% endif %}>{{ choice_value.display_name|replace(" $", "$") }}</option>
                            {%- endfor %}
                        </select>
                    </div>
                </div>
                {%- endfor %}
            </div>
            {%- endif %}
        </div>
    </div>
    {% endfor %}
</div>

    """,
    config=True,
    help="""
    Jinja2 template for constructing profile list shown to the user.
    Used when `profile_list` is set.
    The contents of `profile_list` are passed into the template.
    This should be used to construct the contents of an HTML form. When
    submitted, this form is expected to have an item with the name `profile` and
    the value representing the index of the selected profile in `profile_list`.
    """,
)

    _profile_list = None
    job_prefix = Unicode("jupyterhub_spawner")

    def _init_profile_list(self, profile_list):
        # generate missing slug fields from display_name
        for profile in profile_list:
            if "slug" not in profile:
                profile["slug"] = slugify(profile["display_name"])

        return profile_list

    def _render_options_form(self, profile_list):
        self._profile_list = self._init_profile_list(profile_list)
        profile_form_template = Environment(loader=BaseLoader).from_string(
            self.profile_form_template
        )
        return profile_form_template.render(profile_list=self._profile_list)

    @staticmethod
    def get_price(instance_type, max_retries=3, delay=5):
        # Set the region code and region names
        region_code = 'us-east-1'
        region_names = {
            "us-east-1": "US East (N. Virginia)",
        }
        # Create a session and pricing client using the specified region
        session = boto3.Session(region_name=region_code)
        pricing = session.client('pricing')
        next_token = ''
        # Retry fetching the price for the instance type for a given number(max_retries=3) of attempts
        for attempt in range(max_retries):
            try:
                while True:
                    # Get the pricing information for the specified instance type
                    response = pricing.get_products(
                        ServiceCode='AmazonEC2',
                        Filters=[
                            {'Type': 'TERM_MATCH', 'Field': 'instanceType', 'Value': instance_type},
                            {'Type': 'TERM_MATCH', 'Field': 'location', 'Value': region_names[region_code]},
                            {'Type': 'TERM_MATCH', 'Field': 'operatingSystem', 'Value': 'Linux'},
                            {'Type': 'TERM_MATCH', 'Field': 'tenancy', 'Value': 'Shared'},
                            {'Type': 'TERM_MATCH', 'Field': 'preInstalledSw', 'Value': 'NA'},
                            {'Type': 'TERM_MATCH', 'Field': 'capacitystatus', 'Value': 'Used'}
                        ],
                        MaxResults=100,
                        NextToken=next_token
                    )
                    # Parse the response and extract the price information
                    for product in response['PriceList']:
                        product_obj = json.loads(product)
                        terms = product_obj['terms']
                        # Check if the price is not zero and return the price
                        for term_id, term_info in terms.items():
                            if term_id.startswith("OnDemand"):
                                for sku in term_info.values():
                                    if 'priceDimensions' in sku:
                                        for price_dimension in sku['priceDimensions'].values():
                                            if float(price_dimension['pricePerUnit']['USD']) > 0:
                                                price = float(price_dimension['pricePerUnit']['USD'])
                                                return {"price": "{:.2f}".format(price)}
                    # Check if there is a next token in the response and continue to fetch the price
                    if 'NextToken' in response:
                        next_token = response['NextToken']
                    else:
                        break
                break
            except Exception as e:
                if attempt < max_retries - 1:
                    time.sleep(delay)
                    continue
                else:
                    # Print an error message if the price fetch failed after maximum retries
                    print(f"Failed to fetch the price for {instance_type} after {max_retries} attempts.")
                    return {'price': 'N/A'}

    def get_instance_prices(self, instance_types):
        # Use ThreadPoolExecutor for parallel execution of get_price function for multiple instance types
        with ThreadPoolExecutor() as executor:
            results = executor.map(self.get_price, instance_types)
        # Convert the results to a dictionary with instance type as key and price as value
        return {instance_type: price for instance_type, price in zip(instance_types, results)}

    @property
    def profiles_list(self) -> List[Any]:
        """
        List of profiles to offer for selection by the user.
        Signature is: `List(Dict())`, where each item is a dictionary that has two keys:
        - `display_name`: the human readable display name (should be HTML safe)
        - `slug`: the machine readable slug to identify the profile
          (missing slugs are generated from display_name)
        - `description`: Optional description of this profile displayed to the user.
        - `pclusterslurmspawner_override`: a dictionary with overrides to apply to the PClusterSlurmSpawner
          settings. Each value can be either the final value to change or a callable that
          take the `PClusterSlurmSpawner` instance as parameter and return the final value. This can
          be further overridden by 'profile_options'
        - 'profile_options': A dictionary of sub-options that allow users to further customize the
          selected profile. By default, these are rendered as a dropdown with the label
          provided by `display_name`. Items should have a unique key representing the customization,
          and the value is a dictionary with the following keys:
          - 'display_name': Name used to identify this particular option
          - 'choices': A dictionary containing list of choices for the user to choose from
            to set the value for this particular option. The key is an identifier for this
            choice, and the value is a dictionary with the following possible keys:
            - 'display_name': Human readable display name for this choice.
            - 'default': (optional Bool) True if this is the default selected choice
            - 'pclusterslurmspawner_override': A dictionary with overrides to apply to the PClusterSlurmSpawner
              settings, on top of whatever was applied with the 'pclusterslurmspawner_override' key
              for the profile itself. The key should be the name of the pclusterslurmspawner setting,
              and value can be either the final value or a callable that returns the final
              value when called with the spawner instance as the only parameter.
        - `default`: (optional Bool) True if this is the default selected option
        pclusterslurmspawner setting overrides work in the following manner, with items further in the
        list *replacing* (not merging with) items earlier in the list:
        1. Settings directly set on PClusterSlurmSpawner, via c.PClusterSlurmSpawner.<traitlet_name>
        2. `pclusterslurmspawner_override` in the profile the user has chosen
        3. `pclusterslurmspawner_override` in the specific choices the user has made within the
           profile, applied linearly based on the ordering of the option in the profile
           definition configuration
        Example::
            c.PClusterSlurmSpawner.profile_list = [
                {
                    'display_name': 'Training Env',
                    'slug': 'training-python',
                    'default': True,
                    'profile_options': {
                        'image': {
                            'display_name': 'Image',
                            'choices': {
                                'pytorch': {
                                    'display_name': 'Python 3 Training Notebook',
                                    'pclusterslurmspawner_override': {
                                        'image': 'training/python:2022.01.01'
                                    }
                                },
                                'tf': {
                                    'display_name': 'R 4.2 Training Notebook',
                                    'pclusterslurmspawner_override': {
                                        'image': 'training/r:2021.12.03'
                                    }
                                }
                            }
                        }
                    },
                    'pclusterslurmspawner_override': {
                        'cpu_limit': 1,
                        'mem_limit': '512M',
                    }
                }, {
                    'display_name': 'Python DataScience',
                    'slug': 'datascience-small',
                    'profile_options': {
                        'memory': {
                            'display_name': 'CPUs',
                            'choices': {
                                '2': {
                                    'display_name': '2 CPUs',
                                    'pclusterslurmspawner_override': {
                                        'cpu_limit': 2,
                                        'cpu_guarantee': 1.8,
                                        'node_selectors': {
                                            'node.kubernetes.io/instance-type': 'n1-standard-2'
                                        }
                                    }
                                },
                                '4': {
                                    'display_name': '4 CPUs',
                                    'pclusterslurmspawner_override': {
                                        'cpu_limit': 4,
                                        'cpu_guarantee': 3.5,
                                        'node_selectors': {
                                            'node.kubernetes.io/instance-type': 'n1-standard-4'
                                        }
                                    }
                                }
                            }
                        },
                    },
                    'pclusterslurmspawner_override': {
                        'image': 'datascience/small:label',
                    }
                }, {
                    'display_name': 'DataScience - Medium instance (GPUx2)',
                    'slug': 'datascience-gpu2x',
                    'pclusterslurmspawner_override': {
                        'image': 'datascience/medium:label',
                        'cpu_limit': 48,
                        'mem_limit': '96G',
                        'extra_resource_guarantees': {"nvidia.com/gpu": "2"},
                    }
                }
            ]
        Instead of a list of dictionaries, this could also be a callable that takes as one
        parameter the current spawner instance and returns a list of dictionaries. The
        callable will be called asynchronously if it returns a future, rather than
        a list. Note that the interface of the spawner class is not deemed stable
        across versions, so using this functionality might cause your JupyterHub
        or pclusterslurmspawner upgrades to break.

        In [16]:  sinfo.dataframe.groupby('queue').get_group('mem')
        Out[16]:
                  sinfo_name                label queue  constraint ec2_instance_type  mem  cpu   gpu gpus extra
        6  mem-dy-r6i2xlarge  mem_dy__r6i_2xlarge   mem  r6i2xlarge       r6i.2xlarge   64    4  None   []    {}
        5  mem-dy-m5a4xlarge  mem_dy__m5a_4xlarge   mem  m5a4xlarge       m5a.4xlarge   64    8  None   []    {}
        7  mem-dy-c6a8xlarge  mem_dy__c6a_8xlarge   mem  c6a8xlarge       c6a.8xlarge   64   16  None   []    {}
        """
        records = self.sinfo.dataframe.to_dict("records")
        instance_types = [record["ec2_instance_type"] for record in records]
        cpus = [str(record["vcpu"]) + "CPU" for record in records]
        mems = [str(record["mem"]) + "GB" for record in records]
        instance_prices = self.get_instance_prices(list(set(instance_types)))
        max_instance_length = max(len(instance_type) for instance_type in instance_types)
        max_cpu_length = max(len(cpu) for cpu in cpus)
        max_mem_length = max(len(mem) for mem in mems)
        instance_prices = self.get_instance_prices(list(set(instance_types)))
    
        # Retrieve all projects listed from the file if it exists
        file_path = '/shared/projects_list.txt'
        if os.path.exists(file_path):
            projects = self.read_projects_from_file(file_path)
        else:
            projects = ["No file"]
    
        project_choices = {
            "----": {"display_name": "---- Please select a project ----"},
            **{project: {"display_name": project} for project in projects}
        }
    
        profiles = [
            {
                "display_name": f"CPU",
                "slug": "cpu",
                "ami_name": "Deep Learning",
                "profile_options": {
                    "project": {"display_name": "Project", "choices": project_choices},
                    "instance_types": {"display_name": "Instance Types", "choices": {}},
                },
            },
            {
                "display_name": f"GPU",
                "slug": "gpu",
                "ami_name": "Deep Learning",
                "profile_options": {
                    "instance_types": {"display_name": "Instance Types", "choices": {}},
                },
            },
        ]
    
        for group_record in self.sinfo.dataframe.to_dict("records"):
            sinfo_name = group_record["sinfo_name"]
            instance_type = group_record["ec2_instance_type"]
            instance_price = instance_prices.get(instance_type, {}).get('price', 'N/A')
            mem = group_record["mem"]
            cpu = group_record["vcpu"]
            is_gpu = len(group_record["gpus"]) > 0
            profile_index = 1 if is_gpu else 0
            profile_choices = profiles[profile_index]["profile_options"]["instance_types"]["choices"]
            padded_instance_type = instance_type.ljust(max_instance_length).replace(" ", "&nbsp;")
            padded_cpu = (str(cpu) + "CPU,").ljust(max_cpu_length).replace(" ", "&nbsp;")
            padded_mem = (str(mem) + "GB,").ljust(max_mem_length).replace(" ", "&nbsp;")
            profile_choices[sinfo_name] = dict(
                display_name=f"{padded_instance_type} - {padded_cpu} {padded_mem}",
                pclusterslurmspawner_override=group_record,
            )
            if instance_price != 'N/A':
                profile_choices[sinfo_name]['display_name'] += f"  ${instance_price}/hr"
            else:
                profile_choices[sinfo_name]['display_name'] += ",  N/A"
    
        return profiles

    #-------------------------------------------[ Project list drop-down menu ]---------------------------------------------
    @staticmethod
    def read_projects_from_file(file_path):
        with open(file_path, 'r') as file:
            return [line.strip() for line in file if line.strip()]
        
    def save_project_name(self, project_name):
    # Get the home directory of the user
        homedir = pwd.getpwnam(self.user.name).pw_dir
        # Construct the full path to the file
        file_path = os.path.join(homedir, 'chosen_project.txt')
        # Write the project name to the file
        with open(file_path, 'w') as file:
            file.write(project_name.strip())
    #-------------------------------------------X Project list drop-down menu X---------------------------------------------
    @property
    def sinfo(self):
        sinfo = SInfoTable()
        df = sinfo.dataframe
        df.sort_values(by=["mem", "vcpu", "cpu", "queue"], inplace=True)
        sinfo.dataframe = df
        return sinfo

    @property
    def slurm_spawner_html_table(self):
        table = self.sinfo.dataframe

        return table.to_html(
            table_id="slurm_spawner_table",
            classes="table  table-bordered display dataTable",
            columns=[
                "sinfo_name",
                "queue",
                "constraint",
                "ec2_instance_type",
                "mem",
                "vcpu",
                "gpus",
            ],
            index=False,
        )

    def _options_form_default(self):
        sinfo = SInfoTable()
        table = sinfo.dataframe
        profile_form = None
        try:
            profile_form = self._render_options_form(self.profiles_list)
        except Exception as e:
            self.log.error("Got an error rendering the profiles list")
            self.log.error(e)
        if table.shape[0]:
            first_row: SinfoRow = table.iloc[0]
            defaults = {
                "req_nprocs": str(first_row.vcpu),
                "req_memory": str(first_row.mem),
                "req_runtime": "08:00:00",
                "req_partition": first_row.queue,
                "req_options": "",
                "req_custom_r": "",
                "req_custom_env": "",
                "req_constraint": first_row.constraint,
                "exclusive": True,
                "job_prefix": self.job_prefix,
                # 'slurm_spawner_table': html_table,
                "slurm_spawner_table": "",
                "profile": profile_form,
            }
        else:
            # TODO If there's nothing in the table we have a problem
            defaults = {
                "slurm_spawner_table": "",
                "job_prefix": self.job_prefix,
                # 'slurm_spawner_table': html_table,
                "profile": profile_form,
            }
        form_options = """
    <div class="row">
    <div class="col">
        <div class="form-group">
            {{ slurm_spawner_table }}
        </div>
        {{ profile }}
        <div class="d-flex align-items-center">
            <input type="checkbox" id="exclusive" name="exclusive" value="exclusive" {% if exclusive %}checked{% endif %}>
            <label for="exclusive" class="ml-2">Exclusive (Reserve the entire node)</label>
        </div>
        <div class="form-group">
            <label for="runtime">Runtime (--time)</label>
            <input type="text" class="form-control" value="{{ req_runtime }}" placeholder="{{ req_runtime }}" id="runtime" name="req_runtime"/>
        </div>
        <div class="form-group">
            <label for="options">Options (additional options such as -N 4 for multiple nodes)</label>
            <input type="text" class="form-control" value="{{ req_options }}" placeholder="{{ req_options }}" id="options" name="req_options"/>
        </div>
    </div>
</div>
    """
        rtemplate = Environment(loader=BaseLoader).from_string(form_options)
        return rtemplate.render(**defaults)

    def options_from_form(self, formdata):
        #-------------------------------------------[ Project list drop-down menu ]---------------------------------------------
        # Retrieve the selected profile
        selected_profile = formdata.get("profile", [""])[0]
        project_name = ""
    
        if selected_profile == "gpu":
            # Retrieve the project choice from the CPU profile
            cpu_profile = next((profile for profile in self.profiles_list if profile["slug"] == "cpu"), None)
            if cpu_profile:
                cpu_project_choices = cpu_profile["profile_options"]["project"]["choices"]
                project_choice = formdata.get("profile-option-cpu-project", [""])[0]
                project_name = cpu_project_choices.get(project_choice, {}).get("display_name", "")
    
        # Save the project name
        self.save_project_name(project_name)
        #-------------------------------------------X Project list drop-down menu X---------------------------------------------
        """
        The return from this function is what is read into teh SLURM script
        """
        submission_data = {}
        # self.log.debug('--------------------------------')
        # self.log.debug('FORM DATA')
        # self.log.debug(formdata)
        # self.log.debug('--------------------------------')
        # self.log.debug('USER OPTIONS')
        # self.log.debug(self.user_options)
        # self.log.debug('-------------------------------')
        queue = formdata["profile"][0]
        sinfo_name = formdata[f"profile-option-{queue}-instance_types"][0]
        records = self.sinfo.dataframe.loc[
            self.sinfo.dataframe["sinfo_name"] == sinfo_name
            ].to_dict("records")
        record = records[0]
        for key in formdata.keys():
            form_value = formdata.get(key, [""])
            if not form_value[0]:
                if key in self.user_options:
                    form_value[0] = self.user_options[key]
            submission_data[key] = form_value[0]
        submission_data["req_nprocs"] = str(record["vcpu"])
        submission_data["req_memory"] = str(record["mem"])
        submission_data["req_partition"] = record["queue"]
        submission_data["req_constraint"] = record["constraint"]

        for key in submission_data.keys():
            setattr(self, key, submission_data[key])
        if "req_custom_env" in submission_data.keys():
            l = submission_data["req_custom_env"]
            l = "\n".join(l.splitlines())
            submission_data["req_custom_env"] = l
        if "req_custom_r" in submission_data.keys():
            custom_r = submission_data["req_custom_r"]
            custom_r = os.path.split(custom_r)
            if custom_r[1] == "R":
                custom_r = custom_r[0]
            else:
                custom_r = os.path.join(custom_r[0], custom_r[1])
            submission_data["req_custom_r"] = custom_r
        if "exclusive" in submission_data.keys():
            submission_data["exclusive"] = True
        else:
            submission_data["exclusive"] = False
        self.log.debug("-------------------------------")
        self.log.debug("SUBMISSION_DATA")
        self.log.debug(submission_data)

        return submission_data

    #-------------------------------------------[ Jupyterhub logs in UI ]---------------------------------------------#

    @async_generator
    async def progress(self):
        state = self.get_state()
        dt = datetime.today().strftime("%Y-%m-%d %H:%M:%S")
        homedir = pwd.getpwnam(self.user.name).pw_dir
        instance_type = state['job_status'].split('CONFIGURING ', 1)[-1].strip()
        try:
            # Attempt to create a new directory at the specified path, if it does not exist
            # Also, changes the owner and group of the directory to the current user
            os.system(
                f'runuser {self.user.name} -c "mkdir -p {homedir}/logs; chown {self.user.name}:{self.user.name} {homedir}/logs"'
            )
        except Exception as e:
            pass
    
        # This will run the 'tail -f' command in a subprocess and 
        # capture its output line by line
        command = "tail -f /var/log/slurmctld.log"
        process = await asyncio.create_subprocess_shell(
            command, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
    
        # Regex pattern for ISO 8601 timestamps
        pattern = re.compile(r"\[\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}\]")
    
        while True:
            line = await process.stdout.readline() # This reads one line of output from the 'tail -f' command
            line = line.decode('utf-8').strip()    # Decode bytes to string and remove trailing newline
    
           # Only process lines containing the JobId or the instance type
            if f'JobId={state["job_id"]}' in line or instance_type in line:
                timestamps = pattern.findall(line)
                for timestamp in timestamps:
                    timestamp = timestamp[1:-1]
                    dt_object = datetime.fromisoformat(timestamp)
                    # The year-month-day order, such as the ISO 8601 "YYYY-MM-DD"
                    formatted_dt = dt_object.strftime("%Y-%m-%d %H:%M:%S")
                    line = line.replace(timestamp, formatted_dt)
    
                time.sleep(1)
                if self.state_ispending():
                    message = f"\n{line}" 
                    await yield_({"message": message}) 
                elif self.state_isrunning():
                    message = f"[{dt}]: Job {state['job_id']} cluster job running... waiting to connect"
                    await yield_({"message": message})
                    return
                else:
                    message = f"\n{line}" 
                    await yield_({"message": message})
            else:
                continue  # Skip irrelevant lines
    
            await gen.sleep(10)
    

    #-------------------------------------------X Jupyterhub logs in UI X---------------------------------------------#
# Get private ip
def get_ec2_address(address_type="local-ipv4") -> str:
    response = requests.get(f"http://169.254.169.254/latest/meta-data/{address_type}")
    return response.content.decode("utf-8")

#-------------------------------------------[ Project list drop-down menu ]---------------------------------------------
class VariableSlurmSpawner(PClusterSlurmSpawner, VariableMixin, metaclass=MetaVariableMixin):
    async def options_from_form(self, formdata):
        # Call parent class method to get user options from the form data
        user_options = super().options_from_form(formdata)

        # Retrieve 'profile' from form data, default to empty string if not found
        profile = formdata.get('profile', [''])[0]

        # Construct a dictionary of profile options from form data
        # Keys are form data entries that start with 'profile-option-', values are the corresponding form data values
        profile_options = {k: v[0] for k, v in formdata.items() if k.startswith('profile-option-')}

        # Store profile value in user options
        user_options['profile'] = profile

        # Iterate through profile options and add each to user options
        for key, value in profile_options.items():
            user_options[key] = value

        # Return user options that now includes profile and profile options
        return user_options
#-------------------------------------------X Project list drop-down menu X---------------------------------------------